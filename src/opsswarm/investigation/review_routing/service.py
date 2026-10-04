"""把已提交独立审查编译并保存为确定性路由事实。"""

from collections.abc import Callable
from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.investigation.dynamic_contracts import (
    FirstRoundReviewResult,
    SecondRoundReviewResult,
    ThirdRoundReviewResult,
)
from opsswarm.investigation.review.contracts import HypothesisReviewRecord
from opsswarm.investigation.review_routing.contracts import ReviewRoutingRecord
from opsswarm.investigation.review_routing.persistence import ReviewRoutingStore, utc_now
from opsswarm.investigation.tasks.errors import TaskFactConflict


class _RoundReviewRoutingService:
    """按目标轮次保存或复用确定性路由事实。"""

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        *,
        run_id: UUID,
        lease_token: UUID,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._run_id = run_id
        self._lease_token = lease_token
        self._store = ReviewRoutingStore(factory, clock=clock)

    async def _route_review(self, review: HypothesisReviewRecord) -> ReviewRoutingRecord:
        self._validate_review(review)
        existing = await self._store.get_existing(
            self._run_id, review.id, lease_token=self._lease_token
        )
        if existing is not None:
            self._validate_record(existing, review)
            return existing
        record, _ = await self._store.save(
            self._run_id,
            review.id,
            lease_token=self._lease_token,
            round_id=review.round_id,
            open_hypothesis_id=review.open_hypothesis_id,
            review_sha256=review.content_sha256,
        )
        self._validate_record(record, review)
        return record

    async def _get_completed_review(self, review: HypothesisReviewRecord) -> ReviewRoutingRecord:
        self._validate_review(review)
        record = await self._store.get_existing(
            self._run_id, review.id, lease_token=self._lease_token
        )
        if record is None:
            raise TaskFactConflict("review_routing_missing", "独立审查尚未保存确定性路由")
        self._validate_record(record, review)
        return record

    def _validate_review(self, review: HypothesisReviewRecord) -> None:
        if review.investigation_run_id != self._run_id:
            raise TaskFactConflict("review_routing_source_invalid", "独立审查不属于当前运行")

    @staticmethod
    def _validate_record(record: ReviewRoutingRecord, review: HypothesisReviewRecord) -> None:
        if (
            record.investigation_run_id != review.investigation_run_id
            or record.round_id != review.round_id
            or record.open_hypothesis_id != review.open_hypothesis_id
            or record.hypothesis_review_id != review.id
            or record.review_sha256 != review.content_sha256
        ):
            raise TaskFactConflict(
                "review_routing_source_invalid", "确定性路由与图内独立审查不一致"
            )


class ReviewRoutingService(_RoundReviewRoutingService):
    """路由首轮独立审查，保留既有公开调用契约。"""

    async def route(self, first_round: FirstRoundReviewResult) -> ReviewRoutingRecord:
        return await self._route_review(first_round.hypothesis_review)

    async def get_completed(self, first_round: FirstRoundReviewResult) -> ReviewRoutingRecord:
        return await self._get_completed_review(first_round.hypothesis_review)


class SecondRoundReviewRoutingService(_RoundReviewRoutingService):
    """只路由第二轮开放假设的独立审查。"""

    @staticmethod
    def _review(result: SecondRoundReviewResult) -> HypothesisReviewRecord:
        review = result.follow_up_hypothesis_review
        hypothesis = result.follow_up_hypothesis
        if (
            result.review_route.decision.route != "follow_up"
            or hypothesis is None
            or review is None
            or review.round_id != hypothesis.round_id
            or review.open_hypothesis_id != hypothesis.id
        ):
            raise TaskFactConflict(
                "follow_up_routing_scope_invalid", "第二轮路由缺少已完成的补查审查事实"
            )
        return review

    async def route(self, result: SecondRoundReviewResult) -> ReviewRoutingRecord:
        return await self._route_review(self._review(result))

    async def get_completed(self, result: SecondRoundReviewResult) -> ReviewRoutingRecord:
        return await self._get_completed_review(self._review(result))


class ThirdRoundReviewRoutingService(_RoundReviewRoutingService):
    """只路由达到冻结最大轮次的第三轮开放假设审查。"""

    @staticmethod
    def _review(result: ThirdRoundReviewResult) -> HypothesisReviewRecord:
        plan = result.third_round_plan
        hypothesis = result.third_round_hypothesis
        review = result.third_round_hypothesis_review
        if (
            plan is None
            or plan.plan.assignment.round_number != 3
            or hypothesis is None
            or review is None
            or hypothesis.round_id != plan.round_id
            or review.round_id != hypothesis.round_id
            or review.open_hypothesis_id != hypothesis.id
        ):
            raise TaskFactConflict(
                "third_round_routing_scope_invalid",
                "第三轮路由缺少已完成的第三轮审查事实",
            )
        return review

    async def route(self, result: ThirdRoundReviewResult) -> ReviewRoutingRecord:
        return await self._route_review(self._review(result))

    async def get_completed(self, result: ThirdRoundReviewResult) -> ReviewRoutingRecord:
        return await self._get_completed_review(self._review(result))
