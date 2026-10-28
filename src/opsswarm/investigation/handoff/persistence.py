"""在有效父租约下原子交接；查询和重复提交重新核对业务来源。"""

from collections.abc import Callable
from datetime import datetime
from uuid import UUID, uuid5

from sqlalchemy import String, and_, exists, not_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.incidents.persistence.tables import IncidentRow
from opsswarm.investigation.attempts import InvestigationAttemptStatus, ModelUsageTelemetry
from opsswarm.investigation.budget.tables import CallReservationRow
from opsswarm.investigation.errors import (
    InvestigationRunNotFound,
    WorkItemLeaseLost,
    WorkItemNotFound,
)
from opsswarm.investigation.handoff.contracts import TerminalHandoffRecord
from opsswarm.investigation.handoff.tables import InvestigationTerminalHandoffRow
from opsswarm.investigation.persistence.attempt_settlement import finalize_attempt
from opsswarm.investigation.persistence.lease import normalized_now, require_lease
from opsswarm.investigation.persistence.tables import (
    InvestigationAttemptRow,
    InvestigationRunRow,
    WorkItemRow,
)
from opsswarm.investigation.review_routing.contracts import ReviewRoutingRecord
from opsswarm.investigation.review_routing.persistence import validated_review_routing
from opsswarm.investigation.review_routing.tables import InvestigationHypothesisReviewRouteRow
from opsswarm.investigation.tasks.errors import TaskFactConflict
from opsswarm.investigation.tasks.events import append_task_event
from opsswarm.investigation.tasks.tables import AgentTaskRow, InvestigationRoundRow
from opsswarm.investigation.workflow_versions import DYNAMIC_INVESTIGATION_GRAPH_VERSION


def waiting_status(destination: str) -> str:
    return "await_human" if destination == "await_human" else "await_candidate"


async def terminal_source(
    session: AsyncSession, run: InvestigationRunRow, route_id: UUID, source_sha256: str
) -> ReviewRoutingRecord:
    """终端必须覆盖最高实际轮次，不能把中间补查或未完成工作交出。"""

    row = await session.get(InvestigationHypothesisReviewRouteRow, route_id)
    if (
        run.workflow_version != "v3"
        or run.graph_version != DYNAMIC_INVESTIGATION_GRAPH_VERSION
        or run.data_source_mode != "frozen"
        or row is None
        or row.investigation_run_id != run.id
        or row.content_sha256 != source_sha256
    ):
        raise TaskFactConflict("handoff_source_invalid", "交接缺少同运行、同版本的终端路由")
    route = await validated_review_routing(session, row)
    rounds = list(
        await session.scalars(
            select(InvestigationRoundRow)
            .where(InvestigationRoundRow.investigation_run_id == run.id)
            .order_by(InvestigationRoundRow.round_number)
        )
    )
    unfinished = await session.scalar(
        select(AgentTaskRow.id)
        .where(AgentTaskRow.investigation_run_id == run.id, AgentTaskRow.status != "succeeded")
        .limit(1)
    )
    # 已交接后允许真实新阶段请求在途；其归属仍由完整业务链接约束。
    from opsswarm.investigation.human_prepare.tables import HumanPreparationJobRow
    from opsswarm.investigation.human_review.tables import HumanReviewAttemptRow, HumanReviewJobRow

    human_owner = exists(
        select(HumanReviewAttemptRow.id)
        .join(HumanReviewJobRow, HumanReviewJobRow.id == HumanReviewAttemptRow.job_id)
        .join(
            HumanPreparationJobRow,
            HumanPreparationJobRow.id == HumanReviewJobRow.preparation_job_id,
        )
        .join(
            InvestigationTerminalHandoffRow,
            InvestigationTerminalHandoffRow.id == HumanPreparationJobRow.handoff_id,
        )
        .where(
            HumanReviewAttemptRow.id == CallReservationRow.human_review_attempt_id,
            HumanReviewAttemptRow.investigation_run_id == run.id,
            HumanReviewJobRow.investigation_run_id == run.id,
            HumanPreparationJobRow.investigation_run_id == run.id,
            InvestigationTerminalHandoffRow.investigation_run_id == run.id,
            InvestigationTerminalHandoffRow.investigation_attempt_id
            == CallReservationRow.investigation_attempt_id,
            HumanReviewJobRow.policy_version == "human-input-reassessment-v1",
        )
    )
    from opsswarm.investigation.candidate.tables import CandidateAttemptRow, CandidateJobRow

    candidate_owner = exists(
        select(CandidateAttemptRow.id)
        .join(CandidateJobRow, CandidateJobRow.id == CandidateAttemptRow.job_id)
        .join(
            InvestigationTerminalHandoffRow,
            InvestigationTerminalHandoffRow.investigation_run_id
            == CandidateJobRow.investigation_run_id,
        )
        .where(
            CandidateAttemptRow.id == CallReservationRow.candidate_attempt_id,
            CandidateAttemptRow.investigation_run_id == run.id,
            CandidateJobRow.investigation_run_id == run.id,
            CandidateJobRow.policy_version == "candidate-evaluation-v1",
            CandidateJobRow.source["handoff"]["id"].astext
            == InvestigationTerminalHandoffRow.id.cast(String),
            InvestigationTerminalHandoffRow.investigation_attempt_id
            == CallReservationRow.investigation_attempt_id,
        )
    )
    from opsswarm.incidents.convergence.tables import CandidateIncidentConvergenceRow
    from opsswarm.investigation.reinvestigation.tables import CandidateReinvestigationRow
    from opsswarm.investigation.reinvestigation_coordination.tables import (
        ReflowAttemptRow,
        ReflowJobRow,
    )
    from opsswarm.investigation.settlement.tables import CandidateSettlementRow

    reflow_owner = exists(
        select(ReflowAttemptRow.id)
        .join(ReflowJobRow, ReflowJobRow.id == ReflowAttemptRow.job_id)
        .join(
            CandidateReinvestigationRow,
            CandidateReinvestigationRow.investigation_run_id == ReflowJobRow.investigation_run_id,
        )
        .join(
            CandidateIncidentConvergenceRow,
            CandidateIncidentConvergenceRow.investigation_run_id
            == CandidateReinvestigationRow.investigation_run_id,
        )
        .join(
            CandidateSettlementRow,
            CandidateSettlementRow.investigation_run_id
            == CandidateIncidentConvergenceRow.investigation_run_id,
        )
        .join(
            InvestigationTerminalHandoffRow,
            InvestigationTerminalHandoffRow.id == CandidateSettlementRow.handoff_id,
        )
        .where(
            ReflowAttemptRow.id == CallReservationRow.reinvestigation_attempt_id,
            ReflowAttemptRow.investigation_run_id == run.id,
            ReflowJobRow.investigation_run_id == run.id,
            ReflowJobRow.policy_version == "reinvestigation-coordination-v1",
            ReflowJobRow.source["admission_sha256"].astext
            == CandidateReinvestigationRow.content_sha256,
            ReflowJobRow.source["parent_attempt_id"].astext
            == CallReservationRow.investigation_attempt_id.cast(String),
            InvestigationTerminalHandoffRow.investigation_run_id == run.id,
            InvestigationTerminalHandoffRow.investigation_attempt_id
            == CallReservationRow.investigation_attempt_id,
        )
    )
    from opsswarm.investigation.reinvestigation_dispatch.tables import (
        ReflowDispatchRow,
        ReflowDispatchTaskRow,
    )
    from opsswarm.investigation.reinvestigation_execution.tables import (
        ExecutionAttemptRow,
        ExecutionWorkRow,
    )

    execution_owner = exists(
        select(ExecutionAttemptRow.id)
        .join(ExecutionWorkRow, ExecutionWorkRow.task_id == ExecutionAttemptRow.task_id)
        .join(ReflowDispatchTaskRow, ReflowDispatchTaskRow.id == ExecutionWorkRow.task_id)
        .join(
            ReflowDispatchRow,
            ReflowDispatchRow.investigation_run_id == ExecutionWorkRow.investigation_run_id,
        )
        .join(
            ReflowJobRow, ReflowJobRow.investigation_run_id == ExecutionWorkRow.investigation_run_id
        )
        .where(
            ExecutionAttemptRow.id == CallReservationRow.reflow_task_attempt_id,
            ExecutionAttemptRow.investigation_run_id == run.id,
            ExecutionWorkRow.investigation_run_id == run.id,
            ReflowDispatchTaskRow.investigation_run_id == run.id,
            ReflowDispatchRow.status == "queued",
            ExecutionWorkRow.policy_version == "reinvestigation-task-v1",
            CallReservationRow.reflow_task_id == ExecutionWorkRow.task_id,
            ExecutionWorkRow.dispatch_sha256 == ReflowDispatchRow.content_sha256,
            ExecutionWorkRow.assignment_sha256 == ReflowDispatchTaskRow.assignment_sha256,
            ReflowJobRow.source["parent_attempt_id"].astext
            == CallReservationRow.investigation_attempt_id.cast(String),
        )
    )
    reserved = await session.scalar(
        select(CallReservationRow.id)
        .where(
            CallReservationRow.investigation_run_id == run.id,
            CallReservationRow.status == "reserved",
            or_(
                ~exists(
                    select(InvestigationTerminalHandoffRow.id).where(
                        InvestigationTerminalHandoffRow.investigation_run_id == run.id
                    )
                ),
                not_(
                    or_(
                        and_(
                            CallReservationRow.operation == "human_review",
                            CallReservationRow.human_review_attempt_id.is_not(None),
                            human_owner,
                        ),
                        and_(
                            CallReservationRow.operation.in_(
                                ("collect_reinvestigation", "analyze_reinvestigation")
                            ),
                            CallReservationRow.reflow_task_attempt_id.is_not(None),
                            execution_owner,
                        ),
                        and_(
                            CallReservationRow.operation == "coordinate_reinvestigation",
                            CallReservationRow.reinvestigation_attempt_id.is_not(None),
                            reflow_owner,
                        ),
                        and_(
                            CallReservationRow.operation == "evaluate_candidate",
                            CallReservationRow.candidate_attempt_id.is_not(None),
                            candidate_owner,
                        ),
                    )
                ),
            ),
        )
        .limit(1)
    )
    if (
        route.decision.route == "follow_up"
        or not rounds
        or rounds[-1].id != route.round_id
        or any(item.status != "completed" for item in rounds)
        or unfinished is not None
        or reserved is not None
    ):
        raise TaskFactConflict(
            "handoff_not_terminal", "只有无未完成工作和在途调用的最高轮终端可交接"
        )
    return route


async def validated_handoff(
    session: AsyncSession, row: InvestigationTerminalHandoffRow
) -> TerminalHandoffRecord:
    try:
        record = TerminalHandoffRecord.model_validate(row, from_attributes=True)
    except ValueError as error:
        raise TaskFactConflict("handoff_integrity_violation", "交接内容无法通过结构校验") from error
    run = await session.get(InvestigationRunRow, record.investigation_run_id)
    parent = await session.get(InvestigationAttemptRow, record.investigation_attempt_id)
    work = await session.scalar(
        select(WorkItemRow).where(WorkItemRow.investigation_run_id == record.investigation_run_id)
    )
    if run is None or parent is None or work is None:
        raise TaskFactConflict("handoff_integrity_violation", "交接所属运行、任务或尝试不存在")
    route = await terminal_source(session, run, record.source_review_route_id, record.source_sha256)
    from opsswarm.investigation.human_wait.validation import validated_wait_events

    events = (
        await validated_wait_events(session, record) if record.destination == "await_human" else ()
    )
    cancellation = events[-1] if events and events[-1].action == "cancel" else None
    expected_status = "cancelled" if cancellation else waiting_status(record.destination)
    expected_completion = cancellation.created_at if cancellation else None
    if run.status == "settled":
        from opsswarm.investigation.settlement.parent_state import parent_settlement

        settlement = await parent_settlement(session, run)
        if cancellation is not None or settlement.handoff_id != record.id:
            raise TaskFactConflict("handoff_integrity_violation", "收尾与原交接或取消状态冲突")
        expected_status, expected_completion = "settled", settlement.created_at
    if (
        record.id != uuid5(run.id, "terminal-handoff:v1")
        or record.destination != route.decision.route
        or record.created_at < route.created_at
        or parent.investigation_run_id != run.id
        or parent.work_item_id != work.id
        or parent.status != "handed_off"
        or not parent.started_at <= record.created_at < parent.lease_expires_at
        or parent.finished_at != record.created_at
        or parent.will_retry is not False
        or work.status != "completed"
        or work.completed_at != record.created_at
        or work.lease_token is not None
        or work.locked_by is not None
        or work.locked_until is not None
        or run.status != expected_status
        or run.completed_at != expected_completion
        or run.result is not None
        or run.metrics is not None
    ):
        raise TaskFactConflict("handoff_integrity_violation", "交接与业务状态或来源不一致")
    return record


class TerminalHandoffStore:
    """交接保存独立于旧根因成功结算，提交后不再占用 Worker。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = session_factory

    async def get(self, run_id: UUID) -> TerminalHandoffRecord | None:
        async with self._factory() as session, session.begin():
            # 与人工命令共享父工作项锁，避免取消提交跨越来源与当前状态的读取。
            await session.scalar(
                select(WorkItemRow)
                .where(WorkItemRow.investigation_run_id == run_id)
                .with_for_update(read=True)
            )
            if await session.get(InvestigationRunRow, run_id) is None:
                raise InvestigationRunNotFound(run_id)
            row = await session.scalar(
                select(InvestigationTerminalHandoffRow).where(
                    InvestigationTerminalHandoffRow.investigation_run_id == run_id
                )
            )
            return await validated_handoff(session, row) if row is not None else None

    async def handoff(
        self,
        work_item_id: UUID,
        *,
        lease_token: UUID,
        source_review_route_id: UUID,
        source_sha256: str,
        model_name: str,
        usage: ModelUsageTelemetry,
        clock: Callable[[], datetime],
    ) -> TerminalHandoffRecord:
        async with self._factory() as session, session.begin():
            work = await session.scalar(
                select(WorkItemRow).where(WorkItemRow.id == work_item_id).with_for_update()
            )
            if work is None:
                raise WorkItemNotFound(work_item_id)
            run = await session.get(InvestigationRunRow, work.investigation_run_id)
            if run is None:
                raise InvestigationRunNotFound(work.investigation_run_id)
            parent = await session.scalar(
                select(InvestigationAttemptRow)
                .where(
                    InvestigationAttemptRow.work_item_id == work.id,
                    InvestigationAttemptRow.lease_token == lease_token,
                )
                .with_for_update()
            )
            existing = await session.scalar(
                select(InvestigationTerminalHandoffRow).where(
                    InvestigationTerminalHandoffRow.investigation_run_id == run.id
                )
            )
            if existing is not None:
                if (
                    parent is None
                    or existing.investigation_attempt_id != parent.id
                    or existing.source_review_route_id != source_review_route_id
                    or existing.source_sha256 != source_sha256
                    or parent.model_name != model_name.strip()
                    or any(
                        getattr(parent, name) != value for name, value in usage.model_dump().items()
                    )
                ):
                    raise TaskFactConflict(
                        "handoff_conflict", "交接已存在，拒绝不同来源或尝试的重复命令"
                    )
                return await validated_handoff(session, existing)
            require_lease(work, lease_token, normalized_now(clock))
            lease_deadline = work.locked_until
            incident = await session.scalar(
                select(IncidentRow).where(IncidentRow.id == run.incident_id).with_for_update()
            )
            if (
                run.status != "running"
                or parent is None
                or parent.status != "running"
                or incident is None
                or incident.status != "investigating"
            ):
                raise WorkItemLeaseLost(work.id)
            route = await terminal_source(session, run, source_review_route_id, source_sha256)
            now = normalized_now(clock)
            require_lease(work, lease_token, now)
            if now < route.created_at:
                raise TaskFactConflict("handoff_source_invalid", "交接不能早于来源路由")
            row = InvestigationTerminalHandoffRow(
                id=uuid5(run.id, "terminal-handoff:v1"),
                investigation_run_id=run.id,
                investigation_attempt_id=parent.id,
                source_review_route_id=route.id,
                source_sha256=route.content_sha256,
                version="terminal-handoff-v1",
                destination=route.decision.route,
                created_at=now,
            )
            session.add(row)
            # 原生准入护栏先核对尚未释放的租约；延迟约束核对整笔事务的最终状态。
            await session.flush()
            finalize_attempt(
                parent,
                status=InvestigationAttemptStatus.HANDED_OFF,
                occurred_at=now,
                usage=usage,
                will_retry=False,
                error=None,
                model_name=model_name,
            )
            work.status = "completed"
            work.locked_by = work.locked_until = work.lease_token = None
            work.last_error = None
            work.completed_at = work.updated_at = now
            run.status = waiting_status(route.decision.route)
            run.model_name = model_name.strip()
            run.last_error = None
            run.updated_at = now
            await append_task_event(session, run.id, event_type="run.handed_off", occurred_at=now)
            await session.flush()
            # 租约已清空，仍须按保存的截止时间拒绝写入阶段耗时后的迟到提交。
            if lease_deadline is None or normalized_now(clock) >= lease_deadline:
                raise WorkItemLeaseLost(work.id)
            return TerminalHandoffRecord.model_validate(row, from_attributes=True)
