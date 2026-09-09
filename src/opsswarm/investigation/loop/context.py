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
    AssistantMessage,
    LoopModelInput,
    ToolMessage,
)
from opsswarm.investigation.loop.prompts import PROMPT
from opsswarm.investigation.loop.tools import loop_tool_schema
from opsswarm.investigation.models import SpecialistReport
from opsswarm.investigation.tasks.errors import TaskFactConflict
from opsswarm.investigation.tasks.evidence import require_matching_assignment


def build_loop_context(
    sources: ContextSources,
    policy: ModelBudgetPolicy,
    history: tuple[AssistantMessage | ToolMessage, ...] = (),
) -> ContextPacket:
    if (
        sources.context_policy_version != "evidence-read-v1"
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
    inputs = LoopModelInput(
        messages=(
            TextModelMessage(role="system", content=PROMPT),
            TextModelMessage(
                role="user",
                content=json.dumps(
                    {
                        "incident": sources.request.model_dump(mode="json"),
                        "specialist": task.assignment.role.value,
                        "task_context": {
                            "investigation_run_id": str(sources.run_id),
                            "task_id": str(task.task_id),
                            "assignment": task.assignment.model_dump(mode="json"),
                        },
                        "evidence_catalog": catalog_group(record).model_dump(mode="json"),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            ),
            *history,
        ),
        output_schema=SpecialistReport.model_json_schema(),
        tool_schema=loop_tool_schema(),
    )
    allocation = allocate_input(policy, inputs, prompt_version=LOOP_PROMPT_VERSION)
    return ContextPacket(
        model_input=inputs,
        manifest=ContextManifest(
            version="evidence-read-v1",
            prompt_version=LOOP_PROMPT_VERSION,
            investigation_run_id=sources.run_id,
            task_id=task.task_id,
            role=task.assignment.role.value,
            evidence_policy_version=sources.evidence_policy_version,
            tool_names=("read_evidence",),
            materials=(
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
            ),
            allocation=allocation,
        ),
    )
