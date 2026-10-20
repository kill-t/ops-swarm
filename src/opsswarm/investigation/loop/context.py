"""从业务材料与已提交交互构建专业目录上下文。"""

import json

from opsswarm.investigation.budget.model_contracts import ModelBudgetPolicy, TextModelMessage
from opsswarm.investigation.budget.model_policy import allocate_input
from opsswarm.investigation.context.builder import content_hash
from opsswarm.investigation.context.catalog import catalog_group
from opsswarm.investigation.context.contracts import (
    ContextManifest,
    ContextMaterial,
    ContextPacket,
    ContextSources,
)
from opsswarm.investigation.loop.contracts import (
    LOOP_PROMPT_VERSION,
    RUNBOOK_PROMPT_VERSION,
    AssistantMessage,
    LoopModelInput,
    ToolMessage,
)
from opsswarm.investigation.loop.prompts import PROMPT, RUNBOOK_PROMPT
from opsswarm.investigation.loop.tools import loop_tool_schema
from opsswarm.investigation.models import SpecialistReport
from opsswarm.investigation.runbooks.contracts import RunbookCatalog, RunbookReadDetail
from opsswarm.investigation.runbooks.reader import select_runbooks
from opsswarm.investigation.tasks.errors import TaskFactConflict
from opsswarm.investigation.tasks.evidence import require_matching_assignment


def build_loop_context(
    sources: ContextSources,
    policy: ModelBudgetPolicy,
    history: tuple[AssistantMessage | ToolMessage, ...] = (),
    *,
    fixed_runbook_catalog: RunbookCatalog | None = None,
) -> ContextPacket:
    if (
        sources.context_policy_version
        not in ("evidence-read-v1", "summary-context-v1", "runbook-read-v1")
        or sources.task_id is None
        or len(sources.tasks) != 1
        or sources.tasks[0].task_id != sources.task_id
    ):
        raise TaskFactConflict("context_scope_mismatch", "目录必须绑定新策略的唯一专业任务")
    task = sources.tasks[0]
    record = task.evidence
    require_matching_assignment(record.snapshot, task.assignment)
    if (
        record.investigation_run_id != sources.run_id
        or record.task_id != task.task_id
        or record.snapshot.version != sources.evidence_policy_version
        or task.assignment.service_name != sources.request.service_name
    ):
        raise TaskFactConflict("context_scope_mismatch", "目录材料不属于当前事故任务")
    runbook_policy = sources.context_policy_version == "runbook-read-v1"
    selected_runbooks = select_runbooks(sources.request, record) if runbook_policy else None
    if fixed_runbook_catalog is not None and fixed_runbook_catalog != selected_runbooks:
        raise TaskFactConflict("runbook_version_drift", "已固定的调查手册目录与当前版本不一致")
    runbook_catalog = fixed_runbook_catalog or selected_runbooks
    include_runbook = runbook_catalog is not None and bool(runbook_catalog.entries)
    user_payload = {
        "incident": sources.request.model_dump(mode="json"),
        "specialist": task.assignment.role.value,
        "task_context": {
            "investigation_run_id": str(sources.run_id),
            "task_id": str(task.task_id),
            "assignment": task.assignment.model_dump(mode="json"),
        },
        "evidence_catalog": catalog_group(record).model_dump(mode="json"),
    }
    if runbook_catalog is not None:
        user_payload["runbook_catalog"] = runbook_catalog.model_dump(mode="json")
    inputs = LoopModelInput(
        version="runbook-loop-input-v1" if runbook_policy else "evidence-loop-input-v1",
        messages=(
            TextModelMessage(role="system", content=RUNBOOK_PROMPT if runbook_policy else PROMPT),
            TextModelMessage(
                role="user",
                content=json.dumps(
                    user_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            ),
            *history,
        ),
        output_schema=SpecialistReport.model_json_schema(),
        tool_schema=loop_tool_schema(include_runbook=include_runbook),
    )
    prompt_version = RUNBOOK_PROMPT_VERSION if runbook_policy else LOOP_PROMPT_VERSION
    allocation = allocate_input(policy, inputs, prompt_version=prompt_version)
    materials = [
        ContextMaterial(
            kind="incident_input",
            material_id=sources.request.incident_id,
            version="incident-input-v1",
            content_sha256=content_hash(sources.request),
        ),
        ContextMaterial(
            kind="task_assignment",
            material_id=task.task_id,
            version="task-assignment-v1",
            content_sha256=content_hash(task.assignment),
        ),
        ContextMaterial(
            kind="task_evidence",
            material_id=task.task_id,
            version=record.snapshot.version,
            content_sha256=record.content_sha256,
            evidence_ids=tuple(e.evidence_id for e in record.snapshot.evidence),
        ),
    ]
    if runbook_catalog is not None:
        materials.append(
            ContextMaterial(
                kind="runbook_catalog",
                material_id=runbook_catalog.material_id,
                version=runbook_catalog.version,
                content_sha256=runbook_catalog.content_sha256,
            )
        )
        seen: set[tuple[str, str]] = set()
        for message in history:
            if not isinstance(message, ToolMessage) or message.name != "read_runbook":
                continue
            detail = RunbookReadDetail.model_validate_json(message.content)
            document = detail.document
            identity = (document.runbook_id, document.version)
            entry = next(
                (
                    item
                    for item in runbook_catalog.entries
                    if (item.runbook_id, item.version) == identity
                ),
                None,
            )
            if (
                entry is None
                or entry.material_id != document.material_id
                or entry.title != document.title
                or entry.content_sha256 != document.content_sha256
                or entry.content_bytes != document.content_bytes
                or entry.authority != document.authority
            ):
                raise TaskFactConflict(
                    "runbook_version_drift", "已读取的调查手册正文与固定目录不一致"
                )
            if identity in seen:
                continue
            seen.add(identity)
            materials.append(
                ContextMaterial(
                    kind="runbook_content",
                    material_id=document.material_id,
                    version=f"{document.runbook_id}:{document.version}",
                    content_sha256=document.content_sha256,
                )
            )
    return ContextPacket(
        model_input=inputs,
        manifest=ContextManifest(
            version=sources.context_policy_version,
            prompt_version=prompt_version,
            investigation_run_id=sources.run_id,
            task_id=task.task_id,
            role=task.assignment.role.value,
            workflow_version=sources.workflow_version,
            evidence_policy_version=sources.evidence_policy_version,
            tool_names=("read_evidence", "read_runbook") if include_runbook else ("read_evidence",),
            materials=tuple(materials),
            allocation=allocation,
        ),
    )
