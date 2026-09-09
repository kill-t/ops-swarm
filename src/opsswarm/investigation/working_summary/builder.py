"""将已验证的专业输出投影为工作摘要，不增加事实或自然语言分类。"""

from opsswarm.investigation.budget.contracts import CallReservationRecord
from opsswarm.investigation.context.catalog import catalog_group
from opsswarm.investigation.context.evidence_contracts import ContextEvidenceDetail
from opsswarm.investigation.loop.contracts import AssistantMessage, LoopModelResult, ToolMessage
from opsswarm.investigation.loop.policy import validate_model_result
from opsswarm.investigation.models import SpecialistReport
from opsswarm.investigation.tasks.contracts import TaskAssignment
from opsswarm.investigation.tasks.errors import TaskFactConflict
from opsswarm.investigation.tasks.evidence import TaskEvidenceRecord, require_matching_assignment
from opsswarm.investigation.tasks.results import TaskResultRecord
from opsswarm.investigation.working_summary.contracts import (
    SummaryCallReference,
    TaskWorkingSummary,
)
from opsswarm.investigation.working_summary.hashing import material_hash


def build_summary(
    result: TaskResultRecord,
    evidence: TaskEvidenceRecord,
    assignment: TaskAssignment,
    calls: tuple[CallReservationRecord, ...],
    final: CallReservationRecord,
    output: LoopModelResult,
    history: tuple[AssistantMessage | ToolMessage, ...],
) -> TaskWorkingSummary:
    calls = tuple(sorted(calls, key=lambda c: c.sequence))
    require_matching_assignment(evidence.snapshot, assignment)
    if (
        evidence.task_id != result.task_id
        or evidence.investigation_run_id != result.investigation_run_id
        or result.result.evidence != evidence.snapshot.evidence
        or result.result.evidence_snapshot_id != evidence.snapshot.evidence_snapshot_id
        or result.result.data_source_mode != evidence.snapshot.data_source_mode
        or final.task_attempt_id != result.attempt_id
        or final.status != "succeeded"
        or final.kind != "model"
        or final.operation.value != f"analyze_{assignment.role.value}"
        or not calls
        or final.id not in {c.id for c in calls}
        or any(
            c.task_id != result.task_id
            or c.investigation_run_id != result.investigation_run_id
            or c.status == "reserved"
            for c in calls
        )
    ):
        raise TaskFactConflict("working_summary_source_invalid", "摘要来源不完整或不属于当前任务")
    # 历史只包含已完成的配对，未完成交互不能被摘要掩盖。
    if len(history) % 2:
        raise TaskFactConflict("working_summary_source_invalid", "摘要不能拆分工具消息配对")
    validate_model_result(output, assignment.role.value, history)
    message = output.messages[0]
    assert isinstance(message, AssistantMessage)
    if (
        message.tool_calls[0].name != "SpecialistReport"
        or SpecialistReport.model_validate(message.tool_calls[0].args) != result.result.report
    ):
        raise TaskFactConflict("working_summary_report_mismatch", "最终模型交互与专业结果不一致")
    read_ids = set()
    for index in range(0, len(history), 2):
        request, item = history[index : index + 2]
        if (
            not isinstance(request, AssistantMessage)
            or len(request.tool_calls) != 1
            or not isinstance(item, ToolMessage)
            or request.tool_calls[0].name != "read_evidence"
            or item.name != "read_evidence"
            or item.tool_call_id != request.tool_calls[0].id
        ):
            raise TaskFactConflict("working_summary_source_invalid", "摘要来源消息配对不完整")
        if isinstance(item, ToolMessage):
            detail = ContextEvidenceDetail.model_validate_json(item.content)
            expected = next(
                (
                    e
                    for e in evidence.snapshot.evidence
                    if e.evidence_id == detail.evidence.evidence_id
                ),
                None,
            )
            if (
                detail.investigation_run_id != result.investigation_run_id
                or detail.task_id != result.task_id
                or detail.snapshot_sha256 != evidence.content_sha256
                or detail.snapshot_id != evidence.snapshot.evidence_snapshot_id
                or detail.version != evidence.snapshot.version
                or detail.evidence != expected
                or request.tool_calls[0].args
                != {
                    "evidence_id": detail.evidence.evidence_id,
                    "snapshot_sha256": evidence.content_sha256,
                }
                or not any(
                    c.id == detail.call_id
                    and c.kind == "model"
                    and c.task_attempt_id == result.attempt_id
                    and c.status == "succeeded"
                    and c.invocation_index == index // 2
                    for c in calls
                )
            ):
                raise TaskFactConflict("working_summary_source_invalid", "已读材料与快照不一致")
            read_ids.add(detail.evidence.evidence_id)
    return TaskWorkingSummary(
        investigation_run_id=result.investigation_run_id,
        task_id=result.task_id,
        attempt_id=result.attempt_id,
        assignment=assignment,
        report=result.result.report,
        result_sha256=material_hash(result.result),
        source_call_id=final.id,
        source_result_sha256=material_hash(output),
        evidence_catalog=catalog_group(evidence),
        read_evidence_ids=tuple(sorted(read_ids)),
        calls=tuple(
            SummaryCallReference.model_validate(
                {
                    "call_id": c.id,
                    "attempt_id": c.task_attempt_id,
                    "operation": c.operation.value,
                    "sequence": c.sequence,
                    "invocation_index": c.invocation_index,
                    "kind": c.kind,
                    "status": c.status,
                    "error_code": c.error_code,
                }
            )
            for c in calls
        ),
    )
