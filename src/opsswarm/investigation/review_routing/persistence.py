"""确定性审查路由的来源重建、不可变写入与恢复。"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid5

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.coordination.contracts import CoordinationPolicyRecord
from opsswarm.investigation.coordination.persistence import validated_policy
from opsswarm.investigation.coordination.tables import InvestigationCoordinationPolicyRow
from opsswarm.investigation.persistence.lease import active_investigation_transaction
from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.investigation.review.contracts import HypothesisReviewRecord
from opsswarm.investigation.review.persistence import validated_hypothesis_review
from opsswarm.investigation.review.tables import InvestigationHypothesisReviewRow
from opsswarm.investigation.review_routing.contracts import (
    ReviewRoutingDecision,
    ReviewRoutingRecord,
)
from opsswarm.investigation.review_routing.policy import compile_review_routing_decision
from opsswarm.investigation.review_routing.tables import InvestigationHypothesisReviewRouteRow
from opsswarm.investigation.synthesis.contracts import OpenHypothesisRecord
from opsswarm.investigation.synthesis.persistence import validated_open_hypothesis
from opsswarm.investigation.synthesis.tables import InvestigationOpenHypothesisRow
from opsswarm.investigation.tasks.errors import TaskFactConflict
from opsswarm.investigation.tasks.tables import AgentTaskRow, InvestigationRoundRow
from opsswarm.investigation.workflow_versions import (
    DYNAMIC_ROUTING_GRAPH_VERSIONS,
    DYNAMIC_SECOND_ROUND_ROUTING_GRAPH_VERSIONS,
    DYNAMIC_THIRD_ROUND_ROUTING_GRAPH_VERSIONS,
)


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class _RoutingSources:
    review: HypothesisReviewRecord
    hypothesis: OpenHypothesisRecord
    policy: CoordinationPolicyRecord
    decision: ReviewRoutingDecision


async def _validated_routing_sources(
    session: AsyncSession,
    run: InvestigationRunRow,
    hypothesis_review_id: UUID,
) -> _RoutingSources:
    """重建决定所依赖的审查、假设、策略和既有任务数。"""

    if run.workflow_version != "v3" or run.graph_version not in DYNAMIC_ROUTING_GRAPH_VERSIONS:
        raise TaskFactConflict(
            "review_routing_not_supported", "只有确定性审查路由图可以保存路由事实"
        )
    review_row = await session.get(InvestigationHypothesisReviewRow, hypothesis_review_id)
    if review_row is None or review_row.investigation_run_id != run.id:
        raise TaskFactConflict("review_routing_source_invalid", "路由缺少同运行独立审查")
    review = await validated_hypothesis_review(session, review_row)

    hypothesis_row = await session.get(InvestigationOpenHypothesisRow, review.open_hypothesis_id)
    policy_row = await session.get(InvestigationCoordinationPolicyRow, run.id)
    round_row = await session.get(InvestigationRoundRow, review.round_id)
    if hypothesis_row is None or policy_row is None or round_row is None:
        raise TaskFactConflict("review_routing_source_invalid", "路由来源业务事实不完整")
    hypothesis = await validated_open_hypothesis(session, hypothesis_row)
    policy = validated_policy(policy_row)
    if (
        hypothesis.investigation_run_id != run.id
        or hypothesis.round_id != review.round_id
        or review.open_hypothesis_id != hypothesis.id
        or policy.investigation_run_id != run.id
        or run.coordination_policy_version != policy.version
        or round_row.investigation_run_id != run.id
        or round_row.status != "completed"
        or round_row.completed_at is None
    ):
        raise TaskFactConflict("review_routing_source_invalid", "路由来源归属或完成状态不一致")

    prior_rounds = list(
        await session.scalars(
            select(InvestigationRoundRow)
            .where(
                InvestigationRoundRow.investigation_run_id == run.id,
                InvestigationRoundRow.round_number <= round_row.round_number,
            )
            .order_by(InvestigationRoundRow.round_number)
        )
    )
    if [item.round_number for item in prior_rounds] != list(
        range(1, round_row.round_number + 1)
    ) or any(item.status != "completed" for item in prior_rounds):
        raise TaskFactConflict("review_routing_source_invalid", "路由前的调查轮次未连续完成")
    if (
        round_row.round_number == 2
        and run.graph_version not in DYNAMIC_SECOND_ROUND_ROUTING_GRAPH_VERSIONS
    ):
        raise TaskFactConflict("review_routing_not_supported", "当前图版本不能保存第二轮确定性路由")
    if (
        round_row.round_number == 3
        and run.graph_version not in DYNAMIC_THIRD_ROUND_ROUTING_GRAPH_VERSIONS
    ):
        raise TaskFactConflict("review_routing_not_supported", "当前图版本不能保存第三轮确定性路由")
    existing_task_count = int(
        await session.scalar(
            select(func.count(AgentTaskRow.id))
            .select_from(AgentTaskRow)
            .join(InvestigationRoundRow, InvestigationRoundRow.id == AgentTaskRow.round_id)
            .where(
                AgentTaskRow.investigation_run_id == run.id,
                InvestigationRoundRow.round_number <= round_row.round_number,
            )
        )
        or 0
    )
    decision = compile_review_routing_decision(
        hypothesis.hypothesis,
        review.review,
        policy=policy.policy,
        round_number=round_row.round_number,
        existing_task_count=existing_task_count,
    )
    return _RoutingSources(
        review=review,
        hypothesis=hypothesis,
        policy=policy,
        decision=decision,
    )


async def validated_review_routing(
    session: AsyncSession,
    row: InvestigationHypothesisReviewRouteRow,
) -> ReviewRoutingRecord:
    """从当前不可变来源重新编译并复核一份路由事实。"""

    run = await session.get(InvestigationRunRow, row.investigation_run_id)
    if run is None:
        raise TaskFactConflict("review_routing_source_invalid", "审查路由所属运行不存在")
    sources = await _validated_routing_sources(session, run, row.hypothesis_review_id)
    try:
        record = ReviewRoutingRecord.model_validate(row, from_attributes=True)
    except ValueError as error:
        raise TaskFactConflict(
            "review_routing_integrity_violation", "审查路由内容无法通过校验"
        ) from error
    if (
        record.investigation_run_id != sources.review.investigation_run_id
        or record.round_id != sources.review.round_id
        or record.open_hypothesis_id != sources.hypothesis.id
        or record.review_sha256 != sources.review.content_sha256
        or record.policy_sha256 != sources.policy.content_sha256
        or record.decision != sources.decision
        or row.decision != sources.decision.model_dump(mode="json")
        or record.created_at < sources.review.created_at
        or record.created_at < sources.policy.created_at
    ):
        raise TaskFactConflict("review_routing_source_invalid", "审查路由与重建来源不一致")
    return record


class ReviewRoutingStore:
    """只在有效父租约下提交或复用一份确定性审查路由。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock

    async def get_existing(
        self,
        run_id: UUID,
        hypothesis_review_id: UUID,
        *,
        lease_token: UUID,
    ) -> ReviewRoutingRecord | None:
        async with active_investigation_transaction(
            self._session_factory, run_id, lease_token, self._clock
        ) as (session, _, _, _, _):
            row = await session.scalar(
                select(InvestigationHypothesisReviewRouteRow).where(
                    InvestigationHypothesisReviewRouteRow.investigation_run_id == run_id,
                    InvestigationHypothesisReviewRouteRow.hypothesis_review_id
                    == hypothesis_review_id,
                )
            )
            return await validated_review_routing(session, row) if row is not None else None

    async def save(
        self,
        run_id: UUID,
        hypothesis_review_id: UUID,
        *,
        lease_token: UUID,
        round_id: UUID,
        open_hypothesis_id: UUID,
        review_sha256: str,
    ) -> tuple[ReviewRoutingRecord, bool]:
        async with active_investigation_transaction(
            self._session_factory, run_id, lease_token, self._clock
        ) as (session, parent, _, run, now):
            sources = await _validated_routing_sources(session, run, hypothesis_review_id)
            if (
                sources.review.round_id != round_id
                or sources.hypothesis.id != open_hypothesis_id
                or sources.review.content_sha256 != review_sha256
            ):
                raise TaskFactConflict(
                    "review_routing_source_invalid", "调用方审查结果与持久来源不一致"
                )
            existing = await session.scalar(
                select(InvestigationHypothesisReviewRouteRow).where(
                    InvestigationHypothesisReviewRouteRow.investigation_run_id == run_id,
                    InvestigationHypothesisReviewRouteRow.hypothesis_review_id
                    == hypothesis_review_id,
                )
            )
            if existing is not None:
                return await validated_review_routing(session, existing), False
            decision = sources.decision
            row = InvestigationHypothesisReviewRouteRow(
                id=uuid5(hypothesis_review_id, "hypothesis-review-route:primary"),
                investigation_run_id=run_id,
                round_id=round_id,
                open_hypothesis_id=open_hypothesis_id,
                hypothesis_review_id=hypothesis_review_id,
                investigation_attempt_id=parent.id,
                version=decision.version,
                status=decision.status,
                decision=decision.model_dump(mode="json"),
                review_sha256=sources.review.content_sha256,
                policy_sha256=sources.policy.content_sha256,
                content_sha256=content_hash(decision),
                created_at=now,
            )
            session.add(row)
            await session.flush()
            return await validated_review_routing(session, row), True
