"""把已提交独立审查编译并保存为确定性路由事实。"""

from collections.abc import Callable
from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.investigation.dynamic_contracts import FirstRoundReviewResult
from opsswarm.investigation.review_routing.contracts import ReviewRoutingRecord
from opsswarm.investigation.review_routing.persistence import ReviewRoutingStore, utc_now
from opsswarm.investigation.tasks.errors import TaskFactConflict


class ReviewRoutingService:
    """确定性路由不调用模型、工具，也不创建后续任务。"""

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

    async def route(self, first_round: FirstRoundReviewResult) -> ReviewRoutingRecord:
        self._validate_result(first_round)
        review = first_round.hypothesis_review
        existing = await self._store.get_existing(
            self._run_id, review.id, lease_token=self._lease_token
        )
        if existing is not None:
            self._validate_record(existing, first_round)
            return existing
        record, _ = await self._store.save(
            self._run_id,
            review.id,
            lease_token=self._lease_token,
            round_id=review.round_id,
            open_hypothesis_id=review.open_hypothesis_id,
            review_sha256=review.content_sha256,
        )
        self._validate_record(record, first_round)
        return record

    async def get_completed(self, first_round: FirstRoundReviewResult) -> ReviewRoutingRecord:
        self._validate_result(first_round)
        review = first_round.hypothesis_review
        record = await self._store.get_existing(
            self._run_id, review.id, lease_token=self._lease_token
        )
        if record is None:
            raise TaskFactConflict("review_routing_missing", "独立审查尚未保存确定性路由")
        self._validate_record(record, first_round)
        return record

    def _validate_result(self, first_round: FirstRoundReviewResult) -> None:
        if first_round.hypothesis_review.investigation_run_id != self._run_id:
            raise TaskFactConflict("review_routing_source_invalid", "独立审查不属于当前运行")

    @staticmethod
    def _validate_record(record: ReviewRoutingRecord, first_round: FirstRoundReviewResult) -> None:
        review = first_round.hypothesis_review
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
