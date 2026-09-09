"""结构化工作摘要只投影已持久化事实，不补写调查语义。"""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from opsswarm.investigation.budget.contracts import CallOperation, CallReservationRecord
from opsswarm.investigation.context.evidence_contracts import ContextEvidenceDetail
from opsswarm.investigation.loop.contracts import (
    AssistantMessage,
    LoopModelResult,
    ToolCall,
    ToolMessage,
)
from opsswarm.investigation.models import SpecialistReport
from opsswarm.investigation.tasks.contracts import TaskAssignment
from opsswarm.investigation.tasks.errors import TaskFactConflict
from opsswarm.investigation.tasks.evidence import (
    TaskEvidenceRecord,
    TaskEvidenceSnapshot,
    evidence_hash,
)
from opsswarm.investigation.tasks.results import SpecialistTaskResult, TaskResultRecord
from opsswarm.investigation.working_summary.builder import build_summary
from opsswarm.investigation.working_summary.contracts import WorkingSummaryRecord
from opsswarm.investigation.working_summary.hashing import material_hash
from tests.task_evidence_helpers import material, snapshot


def uid(value: int) -> UUID:
    return UUID(int=value)


@dataclass(frozen=True)
class SummaryFixture:
    result: TaskResultRecord
    evidence: TaskEvidenceRecord
    assignment: TaskAssignment
    calls: tuple[CallReservationRecord, ...]
    final: CallReservationRecord
    output: LoopModelResult
    history: tuple[AssistantMessage | ToolMessage, ...]


def _call(
    *,
    call_id: int,
    run_id: UUID,
    task_id: UUID,
    attempt_id: UUID,
    sequence: int,
    operation: CallOperation,
    kind: str,
    status: str = "succeeded",
    error_code: str | None = None,
    invocation_index: int = 0,
) -> CallReservationRecord:
    now = datetime(2026, 9, 9, 8, 0, tzinfo=UTC)
    return CallReservationRecord.model_validate(
        {
            "id": uid(call_id),
            "investigation_run_id": run_id,
            "investigation_attempt_id": uid(90),
            "task_id": task_id,
            "task_attempt_id": attempt_id,
            "sequence": sequence,
            "invocation_index": invocation_index,
            "operation": operation,
            "kind": kind,
            "status": status,
            "error_code": error_code,
            "reserved_at": now,
            "finished_at": None if status == "reserved" else now,
        }
    )


def fixture() -> SummaryFixture:
    run_id, task_id, attempt_id = uid(1), uid(2), uid(3)
    evidence_call_id, read_model_id = uid(20), uid(22)
    base_snapshot = snapshot()
    first = material()[0]
    unread = first.model_copy(
        update={"evidence_id": "log-unread", "summary": "目录中存在但本次没有读取"}
    )
    evidence_snapshot = TaskEvidenceSnapshot(
        evidence_snapshot_id=base_snapshot.evidence_snapshot_id,
        role=base_snapshot.role,
        service_name=base_snapshot.service_name,
        window_start=base_snapshot.window_start,
        window_end=base_snapshot.window_end,
        evidence=(first, unread),
    )
    evidence = TaskEvidenceRecord(
        task_id=task_id,
        investigation_run_id=run_id,
        attempt_id=attempt_id,
        call_id=evidence_call_id,
        snapshot=evidence_snapshot,
        content_sha256=evidence_hash(evidence_snapshot),
        created_at=datetime(2026, 9, 9, 8, 1, tzinfo=UTC),
    )
    assignment = TaskAssignment(
        task_key="log-analysis",
        role="log",
        goal="核对日志中的支持线索、反证和未解问题",
        service_name=evidence_snapshot.service_name,
        window_start=evidence_snapshot.window_start,
        window_end=evidence_snapshot.window_end,
        evidence_snapshot_id=evidence_snapshot.evidence_snapshot_id,
    )
    report = SpecialistReport(
        specialist="log",
        summary="完成日志调查",
        findings=(
            "支持：请求失败与发布后的异常日志同时出现",
            "反证：未观察到数据库连接耗尽",
            "未解问题：仍需综合指标确认影响范围",
        ),
        evidence_ids=(first.evidence_id,),
        confidence=0.73,
    )
    task_result = SpecialistTaskResult(
        evidence_snapshot_id=evidence_snapshot.evidence_snapshot_id,
        report=report,
        evidence=evidence_snapshot.evidence,
    )
    result = TaskResultRecord(
        task_id=task_id,
        investigation_run_id=run_id,
        attempt_id=attempt_id,
        result=task_result,
        created_at=datetime(2026, 9, 9, 8, 2, tzinfo=UTC),
    )

    failed_attempt = _call(
        call_id=19,
        run_id=run_id,
        task_id=task_id,
        attempt_id=uid(4),
        sequence=1,
        operation=CallOperation.ANALYZE_LOG,
        kind="model",
        status="unknown",
        error_code="parent_attempt_ended",
    )
    evidence_query = _call(
        call_id=20,
        run_id=run_id,
        task_id=task_id,
        attempt_id=attempt_id,
        sequence=2,
        operation=CallOperation.QUERY_LOGS,
        kind="tool",
    )
    read_model = _call(
        call_id=22,
        run_id=run_id,
        task_id=task_id,
        attempt_id=attempt_id,
        sequence=3,
        operation=CallOperation.ANALYZE_LOG,
        kind="model",
    )
    read_tool = _call(
        call_id=23,
        run_id=run_id,
        task_id=task_id,
        attempt_id=attempt_id,
        sequence=4,
        operation=CallOperation.READ_LOG_EVIDENCE,
        kind="tool",
    )
    final = _call(
        call_id=24,
        run_id=run_id,
        task_id=task_id,
        attempt_id=attempt_id,
        sequence=5,
        operation=CallOperation.ANALYZE_LOG,
        kind="model",
        invocation_index=1,
    )
    arguments = {
        "evidence_id": first.evidence_id,
        "snapshot_sha256": evidence.content_sha256,
    }
    read_request = AssistantMessage(
        content="",
        tool_calls=(ToolCall(id="read-1", name="read_evidence", args=arguments),),
    )
    detail = ContextEvidenceDetail(
        investigation_run_id=run_id,
        call_id=read_model_id,
        task_id=task_id,
        version=evidence_snapshot.version,
        snapshot_id=evidence_snapshot.evidence_snapshot_id,
        snapshot_sha256=evidence.content_sha256,
        evidence=first,
    )
    read_response = ToolMessage(
        content=detail.model_dump_json(),
        tool_call_id="read-1",
        name="read_evidence",
    )
    report_call = AssistantMessage(
        content="",
        tool_calls=(
            ToolCall(id="report-2", name="SpecialistReport", args=report.model_dump(mode="json")),
        ),
    )
    report_receipt = ToolMessage(
        content="结构化专业报告已接收",
        tool_call_id="report-2",
        name="SpecialistReport",
    )
    return SummaryFixture(
        result=result,
        evidence=evidence,
        assignment=assignment,
        # 故意倒序，证明摘要身份不依赖仓储返回顺序。
        calls=(final, read_tool, read_model, evidence_query, failed_attempt),
        final=final,
        output=LoopModelResult(messages=(report_call, report_receipt)),
        history=(read_request, read_response),
    )


def _build(value: SummaryFixture, **changes):
    fields = {
        "result": value.result,
        "evidence": value.evidence,
        "assignment": value.assignment,
        "calls": value.calls,
        "final": value.final,
        "output": value.output,
        "history": value.history,
    }
    fields.update(changes)
    return build_summary(**fields)


def test_summary_preserves_report_and_separates_catalog_from_actual_reads():
    value = fixture()

    summary = _build(value)

    assert summary.report == value.result.result.report
    assert summary.report.findings == (
        "支持：请求失败与发布后的异常日志同时出现",
        "反证：未观察到数据库连接耗尽",
        "未解问题：仍需综合指标确认影响范围",
    )
    assert {entry.evidence_id for entry in summary.evidence_catalog.entries} == {
        value.evidence.snapshot.evidence[0].evidence_id,
        "log-unread",
    }
    assert summary.read_evidence_ids == (value.evidence.snapshot.evidence[0].evidence_id,)
    assert [call.sequence for call in summary.calls] == [1, 2, 3, 4, 5]
    assert summary.calls[0].status == "unknown"
    assert summary.calls[0].error_code == "parent_attempt_ended"
    assert summary.calls[-1].invocation_index == 1
    assert summary.calls[-1].kind == "model"
    assert summary.result_sha256 == material_hash(value.result.result)
    assert summary.source_result_sha256 == material_hash(value.output)


@pytest.mark.parametrize(
    "case",
    [
        "foreign_task_call",
        "wrong_snapshot",
        "reserved_call",
        "different_report",
        "broken_pair",
        "wrong_final_operation",
    ],
)
def test_summary_rejects_untrusted_or_incomplete_sources(case):
    value = fixture()
    changes = {}
    if case == "foreign_task_call":
        changes["calls"] = (
            value.calls[0].model_copy(update={"task_id": uid(999)}),
            *value.calls[1:],
        )
    elif case == "wrong_snapshot":
        response = value.history[1]
        assert isinstance(response, ToolMessage)
        detail = ContextEvidenceDetail.model_validate_json(response.content)
        changes["history"] = (
            value.history[0],
            response.model_copy(
                update={
                    "content": detail.model_copy(
                        update={"snapshot_sha256": "0" * 64}
                    ).model_dump_json()
                }
            ),
        )
    elif case == "reserved_call":
        changes["calls"] = (
            value.calls[0].model_copy(update={"status": "reserved", "error_code": None}),
            *value.calls[1:],
        )
    elif case == "different_report":
        report = value.result.result.report.model_copy(update={"summary": "另一份专业结论"})
        request = value.output.messages[0]
        assert isinstance(request, AssistantMessage)
        call = request.tool_calls[0].model_copy(update={"args": report.model_dump(mode="json")})
        changes["output"] = value.output.model_copy(
            update={
                "messages": (
                    request.model_copy(update={"tool_calls": (call,)}),
                    value.output.messages[1],
                )
            }
        )
    elif case == "broken_pair":
        response = value.history[1]
        assert isinstance(response, ToolMessage)
        changes["history"] = (
            value.history[0],
            response.model_copy(update={"tool_call_id": "another-read"}),
        )
    else:
        final = value.final.model_copy(update={"operation": CallOperation.ANALYZE_METRIC})
        changes.update(
            {
                "final": final,
                "calls": tuple(final if call.id == final.id else call for call in value.calls),
            }
        )

    with pytest.raises(TaskFactConflict):
        _build(value, **changes)


def test_working_summary_record_rejects_tampered_hash():
    value = fixture()
    summary = _build(value)
    record = WorkingSummaryRecord(
        task_id=summary.task_id,
        investigation_run_id=summary.investigation_run_id,
        attempt_id=summary.attempt_id,
        version=summary.version,
        summary=summary,
        content_sha256=material_hash(summary),
        created_at=datetime(2026, 9, 9, 8, 3, tzinfo=UTC),
    )

    assert record.summary == summary
    with pytest.raises(ValidationError, match="散列"):
        WorkingSummaryRecord.model_validate(
            {**record.model_dump(mode="json"), "content_sha256": "0" * 64}
        )


def test_summary_rejects_oversized_projection_without_trimming():
    value = fixture()
    oversized = value.assignment.model_copy(update={"goal": "超" * 131072})

    with pytest.raises(ValidationError, match="128 KiB"):
        _build(value, assignment=oversized)
