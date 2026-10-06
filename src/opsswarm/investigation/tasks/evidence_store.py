"""在有效任务尝试下保存和复用已成功取证的不可变材料。"""

from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.investigation.budget.tables import CallReservationRow
from opsswarm.investigation.models import EvidenceKind, EvidenceObservation
from opsswarm.investigation.persistence.lease import active_investigation_transaction
from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.investigation.tasks.contracts import TaskAssignment
from opsswarm.investigation.tasks.errors import TaskFactConflict
from opsswarm.investigation.tasks.events import append_task_event
from opsswarm.investigation.tasks.evidence import (
    LIVE_EVIDENCE_POLICIES,
    EvidencePolicyVersion,
    TaskEvidenceRecord,
    TaskEvidenceSnapshot,
    evidence_hash,
    live_snapshot_id,
    require_matching_assignment,
)
from opsswarm.investigation.tasks.evidence_tables import AgentTaskEvidenceRow
from opsswarm.investigation.tasks.results import SpecialistTaskResult
from opsswarm.investigation.tasks.tables import AgentTaskAttemptRow, AgentTaskRow


class TaskEvidenceStore:
    def __init__(
        self, factory: async_sessionmaker[AsyncSession], *, clock: Callable[[], datetime]
    ) -> None:
        self._factory = factory
        self._clock = clock

    async def validate_result(
        self, run_id: UUID, task_id: UUID, result: SpecialistTaskResult, *, lease_token: UUID
    ) -> None:
        async with active_investigation_transaction(
            self._factory, run_id, lease_token, self._clock
        ) as (session, _, _, _, _):
            task = await session.get(AgentTaskRow, task_id)
            if task is None or task.investigation_run_id != run_id:
                raise TaskFactConflict("task_not_found", "专业任务不存在或不属于该运行")
            await require_result_evidence(session, run_id, task, result)

    async def read_for_attempt(
        self, run_id: UUID, task_id: UUID, attempt_id: UUID, *, lease_token: UUID
    ) -> TaskEvidenceRecord | None:
        async with active_investigation_transaction(
            self._factory, run_id, lease_token, self._clock
        ) as (session, parent, _, run, _):
            assignment = await _current_assignment(session, run, parent.id, task_id, attempt_id)
            row = await session.get(AgentTaskEvidenceRow, task_id)
            return validated_record(row, assignment, run) if row else None

    async def save(
        self,
        run_id: UUID,
        task_id: UUID,
        attempt_id: UUID,
        *,
        lease_token: UUID,
        evidence: Sequence[EvidenceObservation],
    ) -> TaskEvidenceRecord:
        async with active_investigation_transaction(
            self._factory, run_id, lease_token, self._clock
        ) as (session, parent, _, run, now):
            assignment = await _current_assignment(session, run, parent.id, task_id, attempt_id)
            assert assignment.evidence_snapshot_id is not None
            snapshot = TaskEvidenceSnapshot(
                version=cast(EvidencePolicyVersion, run.evidence_policy_version),
                data_source_mode="live" if run.data_source_mode == "live" else "frozen",
                evidence_snapshot_id=assignment.evidence_snapshot_id,
                role=assignment.role,
                service_name=assignment.service_name,
                window_start=assignment.window_start,
                window_end=assignment.window_end,
                evidence=tuple(sorted(evidence, key=lambda e: e.evidence_id)),
            )
            row = await session.get(AgentTaskEvidenceRow, task_id)
            if row is not None:
                record = validated_record(row, assignment, run)
                if record.snapshot != snapshot:
                    raise TaskFactConflict("task_evidence_conflict", "不能覆盖已提交的取证快照")
                return record
            operation = {"log": "query_logs", "metric": "query_metrics", "change": "list_changes"}[
                assignment.role.value
            ]
            call = await session.scalar(
                select(CallReservationRow)
                .where(
                    CallReservationRow.investigation_run_id == run_id,
                    CallReservationRow.task_id == task_id,
                    CallReservationRow.task_attempt_id == attempt_id,
                    CallReservationRow.investigation_attempt_id == parent.id,
                    CallReservationRow.operation == operation,
                    CallReservationRow.status == "succeeded",
                )
                .order_by(CallReservationRow.sequence.desc())
                .limit(1)
            )
            if call is None:
                raise TaskFactConflict(
                    "task_evidence_call_required", "取证快照需要本尝试的成功工具调用"
                )
            row = AgentTaskEvidenceRow(
                task_id=task_id,
                investigation_run_id=run_id,
                attempt_id=attempt_id,
                call_id=call.id,
                snapshot=snapshot.model_dump(mode="json"),
                content_sha256=evidence_hash(snapshot),
                created_at=now,
            )
            session.add(row)
            await append_task_event(
                session, run_id, task_id=task_id, event_type="task.evidence_saved", occurred_at=now
            )
            await session.flush()
            return validated_record(row, assignment, run)


async def _current_assignment(
    session: AsyncSession,
    run: InvestigationRunRow,
    parent_id: UUID,
    task_id: UUID,
    attempt_id: UUID,
) -> TaskAssignment:
    if (run.evidence_policy_version, run.data_source_mode) not in (
        ("task-evidence-v1", "frozen"),
        ("task-evidence-live-v1", "live"),
        ("task-evidence-live-mcp-log-v1", "live"),
    ):
        raise TaskFactConflict("task_evidence_policy_mismatch", "运行未启用受支持的取证策略")
    task = await session.get(AgentTaskRow, task_id)
    attempt = await session.get(AgentTaskAttemptRow, attempt_id)
    if (
        task is None
        or task.investigation_run_id != run.id
        or task.status != "running"
        or attempt is None
        or attempt.task_id != task_id
        or attempt.investigation_run_id != run.id
        or attempt.investigation_attempt_id != parent_id
        or attempt.status != "running"
        or attempt.attempt_number != task.attempt_count
    ):
        raise TaskFactConflict("task_evidence_attempt_invalid", "取证需要当前任务的有效执行尝试")
    assignment = TaskAssignment.model_validate(task.assignment)
    if assignment.evidence_snapshot_id is None:
        raise TaskFactConflict("task_evidence_scope_missing", "任务没有固定证据范围")
    require_run_assignment(run, assignment)
    return assignment


def require_run_assignment(run: InvestigationRunRow, assignment: TaskAssignment) -> None:
    if run.data_source_mode != "live":
        return
    cutoff = run.observation_cutoff_at
    duration = (
        timedelta(hours=6) if assignment.role is EvidenceKind.CHANGE else timedelta(minutes=15)
    )
    if (
        run.evidence_policy_version not in LIVE_EVIDENCE_POLICIES
        or cutoff is None
        or assignment.window_end != cutoff
        or assignment.window_start != cutoff - duration
        or assignment.evidence_snapshot_id != live_snapshot_id(run.id, cutoff)
    ):
        raise TaskFactConflict("task_evidence_scope_mismatch", "现场任务与运行固定窗口不一致")


def validated_record(
    row: AgentTaskEvidenceRow, assignment: TaskAssignment, run: InvestigationRunRow
) -> TaskEvidenceRecord:
    try:
        record = TaskEvidenceRecord.model_validate(row, from_attributes=True)
    except ValueError:
        raise TaskFactConflict("task_evidence_corrupt", "取证快照内容完整性校验失败") from None
    require_run_assignment(run, assignment)
    if (
        record.snapshot.version != run.evidence_policy_version
        or record.snapshot.data_source_mode != run.data_source_mode
    ):
        raise TaskFactConflict("task_evidence_policy_mismatch", "快照与运行取证策略不一致")
    require_matching_assignment(record.snapshot, assignment)
    return record


async def require_result_evidence(
    session: AsyncSession, run_id: UUID, task: AgentTaskRow, result: SpecialistTaskResult
) -> None:
    run = await session.get(InvestigationRunRow, run_id)
    if run is None or run.evidence_policy_version is None:
        return
    row = await session.get(AgentTaskEvidenceRow, task.id)
    if row is None:
        raise TaskFactConflict("task_evidence_required", "专业结果缺少已提交的取证快照")
    record = validated_record(row, TaskAssignment.model_validate(task.assignment), run)
    if (
        record.snapshot.evidence_snapshot_id != result.evidence_snapshot_id
        or record.snapshot.data_source_mode != result.data_source_mode
        or record.snapshot.role is not result.report.specialist
        or record.snapshot.evidence != result.evidence
    ):
        raise TaskFactConflict("task_evidence_result_mismatch", "专业结果与已保存取证材料不一致")
