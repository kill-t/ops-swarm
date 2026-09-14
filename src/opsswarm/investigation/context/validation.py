"""核对图传入材料与业务快照，不分配预算或改变序列化。"""

import json
from dataclasses import dataclass

from opsswarm.investigation.budget.contracts import CallOperation
from opsswarm.investigation.budget.model_contracts import (
    ModelInput,
)
from opsswarm.investigation.budget.policy import operation_role
from opsswarm.investigation.context.contracts import (
    ContextMaterial,
    ContextSources,
    ContextTaskMaterial,
    TaskContextInput,
)
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.models import (
    EvidenceKind,
    EvidenceObservation,
    InvestigationConclusion,
    SpecialistReport,
)
from opsswarm.investigation.synthesis.contracts import OpenHypothesisProposal
from opsswarm.investigation.tasks.errors import TaskFactConflict
from opsswarm.investigation.tasks.evidence import require_matching_assignment


@dataclass(frozen=True)
class ValidatedContext:
    role: EvidenceKind | None
    tasks: list[ContextTaskMaterial]
    evidence: list[EvidenceObservation]
    reports: list[SpecialistReport]
    materials: list[ContextMaterial]


def validate_context(
    supplied: ModelInput, sources: ContextSources, operation: CallOperation
) -> ValidatedContext:
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
    schema = (
        SpecialistReport
        if role
        else OpenHypothesisProposal
        if sources.workflow_version == "v3"
        else InvestigationConclusion
    )
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
            or len({t.assignment.role for t in tasks}) != len(tasks)
            or (
                sources.workflow_version == "v2"
                and ({t.assignment.role for t in tasks} != set(EvidenceKind) or len(tasks) != 3)
            )
            or (sources.workflow_version == "v3" and not 1 <= len(tasks) <= 3)
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
                raise TaskFactConflict("context_result_required", "综合需要与取证一致的已提交报告")
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
    return ValidatedContext(role, tasks, evidence, reports, materials)
