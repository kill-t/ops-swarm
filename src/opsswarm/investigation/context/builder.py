"""从已校验的业务材料确定性组装输入，不执行 SQL、HTTP 或模型调用。"""

import hashlib
import json

from pydantic import BaseModel

from opsswarm.investigation.budget.contracts import CallOperation
from opsswarm.investigation.budget.model_contracts import (
    ModelBudgetPolicy,
    ModelInput,
    TextModelMessage,
)
from opsswarm.investigation.budget.model_policy import allocate_input
from opsswarm.investigation.budget.policy import operation_role
from opsswarm.investigation.context.contracts import (
    CONTEXT_PROMPT_VERSION,
    ContextManifest,
    ContextMaterial,
    ContextPacket,
    ContextSources,
    TaskContextInput,
)
from opsswarm.investigation.context.prompts import SPECIALIST_PROMPT, SYNTHESIS_PROMPT
from opsswarm.investigation.models import (
    EvidenceKind,
    EvidenceObservation,
    InvestigationConclusion,
    SpecialistReport,
)
from opsswarm.investigation.tasks.errors import TaskFactConflict
from opsswarm.investigation.tasks.evidence import require_matching_assignment


def content_hash(value: BaseModel) -> str:
    """材料引用使用完整内容散列，JSON 对象顺序不影响身份。"""

    return hashlib.sha256(
        json.dumps(
            value.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


class ContextBuilder:
    def build(
        self,
        supplied: ModelInput,
        sources: ContextSources,
        operation: CallOperation,
        policy: ModelBudgetPolicy,
    ) -> ContextPacket:
        role_name = operation_role(operation)
        role = EvidenceKind(role_name) if role_name else None
        if operation not in (
            CallOperation.ANALYZE_LOG,
            CallOperation.ANALYZE_METRIC,
            CallOperation.ANALYZE_CHANGE,
            CallOperation.SYNTHESIZE,
        ):
            raise TaskFactConflict("context_operation_invalid", "只有模型调查接受上下文")
        try:
            if len(supplied.messages) != 2 or tuple(m.role for m in supplied.messages) != (
                "system",
                "user",
            ):
                raise ValueError("上下文必须由系统指令和结构化任务输入组成")
            payload = json.loads(supplied.messages[1].content)
            if not isinstance(payload, dict):
                raise ValueError("任务输入必须是对象")
            payload.pop("task_context", None)
            payload.pop("output_json_schema", None)
            inputs = TaskContextInput.model_validate(payload)
        except ValueError:
            raise TaskFactConflict("context_input_invalid", "无法解析受支持的任务输入") from None
        schema = SpecialistReport if role else InvestigationConclusion
        if supplied.output_schema != schema.model_json_schema():
            raise TaskFactConflict("context_contract_mismatch", "角色输出契约不匹配")
        if inputs.incident != sources.request or inputs.specialist != role:
            raise TaskFactConflict("context_scope_mismatch", "事故输入或专业角色与持久材料不一致")
        tasks = sorted(sources.tasks, key=lambda t: t.assignment.role.value)
        if (
            role
            and (
                len(tasks) != 1
                or tasks[0].task_id != sources.task_id
                or tasks[0].assignment.role != role
            )
        ) or (
            role is None
            and (
                sources.task_id is not None
                or {t.assignment.role for t in tasks} != set(EvidenceKind)
                or len(tasks) != 3
            )
        ):
            raise TaskFactConflict("context_scope_mismatch", "上下文任务范围不符合角色")
        materials = [
            ContextMaterial(
                kind="incident_input",
                material_id=sources.request.incident_id,
                version="incident-input-v1",
                content_sha256=content_hash(sources.request),
            )
        ]
        evidence: list[EvidenceObservation] = []
        reports: list[SpecialistReport] = []
        for task in tasks:
            record = task.evidence
            require_matching_assignment(record.snapshot, task.assignment)
            if (
                record.investigation_run_id != sources.run_id
                or record.task_id != task.task_id
                or record.snapshot.version != sources.evidence_policy_version
                or task.assignment.service_name != sources.request.service_name
            ):
                raise TaskFactConflict("context_scope_mismatch", "证据不属于当前事故任务")
            evidence.extend(record.snapshot.evidence)
            materials.extend(
                (
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
                )
            )
            if role is None:
                result = task.result
                if (
                    result is None
                    or result.investigation_run_id != sources.run_id
                    or result.task_id != task.task_id
                    or result.result.evidence != record.snapshot.evidence
                    or result.result.evidence_snapshot_id != record.snapshot.evidence_snapshot_id
                    or result.result.data_source_mode != record.snapshot.data_source_mode
                    or result.result.report.specialist != task.assignment.role
                ):
                    raise TaskFactConflict(
                        "context_result_required", "综合需要与取证一致的已提交报告"
                    )
                reports.append(result.result.report)
                materials.append(
                    ContextMaterial(
                        kind="task_result",
                        material_id=task.task_id,
                        version="specialist-result-v1",
                        content_sha256=content_hash(result.result),
                        evidence_ids=result.result.report.evidence_ids,
                    )
                )
        evidence.sort(key=lambda e: e.evidence_id)
        if (
            len({e.evidence_id for e in inputs.evidence}) != len(inputs.evidence)
            or len({e.evidence_id for e in evidence}) != len(evidence)
            or sorted(inputs.evidence, key=lambda e: e.evidence_id) != evidence
            or sorted(inputs.reports, key=lambda r: r.specialist.value) != reports
        ):
            raise TaskFactConflict("context_material_mismatch", "图输入与持久证据或报告不一致")
        assembled: dict[str, object] = {
            "incident": sources.request.model_dump(mode="json"),
            "evidence": [e.model_dump(mode="json") for e in evidence],
            "task_context": {
                "investigation_run_id": str(sources.run_id),
                "tasks": [
                    {"task_id": str(t.task_id), **t.assignment.model_dump(mode="json")}
                    for t in tasks
                ],
                "completion_requirement": "输出引用输入证据的结构化专业报告"
                if role
                else "综合已提交报告，证据不足时保留未确定判断",
            },
        }
        if role:
            assembled["specialist"] = role.value
        else:
            assembled["reports"] = [r.model_dump(mode="json") for r in reports]
        prompt = SPECIALIST_PROMPT if role else SYNTHESIS_PROMPT
        if supplied.structured_output_method == "json_mode":
            prompt += " 必须只输出符合下方 JSON Schema 的 JSON 对象。"
            assembled["output_json_schema"] = supplied.output_schema
        model_input = ModelInput(
            messages=(
                TextModelMessage(role="system", content=prompt),
                TextModelMessage(
                    role="user",
                    content=json.dumps(
                        assembled, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                    ),
                ),
            ),
            output_schema=supplied.output_schema,
            tool_schema=supplied.tool_schema,
            structured_output_method=supplied.structured_output_method,
        )
        allocation = allocate_input(policy, model_input, prompt_version=CONTEXT_PROMPT_VERSION)
        return ContextPacket(
            model_input=model_input,
            manifest=ContextManifest(
                investigation_run_id=sources.run_id,
                task_id=sources.task_id,
                role=role.value if role else "synthesis",
                evidence_policy_version=sources.evidence_policy_version,
                materials=tuple(materials),
                allocation=allocation,
            ),
        )
