"""把确定性补查路由提交为第二轮协调计划。"""

from collections.abc import Callable
from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.investigation.coordination.contracts import CoordinationPlanRecord
from opsswarm.investigation.coordination.persistence import CoordinationPlanStore, utc_now
from opsswarm.investigation.dynamic_contracts import (
    FirstRoundRoutingResult,
    SecondRoundRoutingResult,
)
from opsswarm.investigation.follow_up_planning.policy import compile_follow_up_proposal
from opsswarm.investigation.review_routing.contracts import ReviewRoutingRecord
from opsswarm.investigation.tasks.errors import TaskFactConflict


class _ReviewRoutePlanService:
    """不调用模型或工具，只消费已重建通过的目标轮次路由。"""

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
        self._store = CoordinationPlanStore(factory, clock=clock)

    async def _plan_route(self, route: ReviewRoutingRecord) -> CoordinationPlanRecord | None:
        self._validate_route(route)
        if route.decision.route != "follow_up":
            existing = await self._store.get_follow_up_plan(
                self._run_id,
                lease_token=self._lease_token,
                source_review_route_id=route.id,
            )
            if existing is not None:
                raise TaskFactConflict("follow_up_source_invalid", "非补查路由不能绑定后续协调计划")
            return None
        record, _ = await self._store.create_follow_up_plan(
            self._run_id,
            lease_token=self._lease_token,
            source_review_route_id=route.id,
        )
        self._validate_record(record, route)
        return record

    async def _get_completed_route(
        self, route: ReviewRoutingRecord
    ) -> CoordinationPlanRecord | None:
        self._validate_route(route)
        if route.decision.route != "follow_up":
            existing = await self._store.get_follow_up_plan(
                self._run_id,
                lease_token=self._lease_token,
                source_review_route_id=route.id,
            )
            if existing is not None:
                raise TaskFactConflict("follow_up_source_invalid", "非补查路由不能绑定后续协调计划")
            return None
        record = await self._store.get_follow_up_plan(
            self._run_id,
            lease_token=self._lease_token,
            source_review_route_id=route.id,
        )
        if record is None:
            raise TaskFactConflict("follow_up_plan_missing", "补查路由尚未保存下一轮计划")
        self._validate_record(record, route)
        return record

    def _validate_route(self, route: ReviewRoutingRecord) -> None:
        if route.investigation_run_id != self._run_id:
            raise TaskFactConflict("follow_up_source_invalid", "补查路由不属于当前运行")

    @staticmethod
    def _validate_record(
        record: CoordinationPlanRecord,
        route: ReviewRoutingRecord,
    ) -> None:
        if (
            record.investigation_run_id != route.investigation_run_id
            or record.source_review_route_id != route.id
            or record.source_call_id is not None
            or record.plan.proposal != compile_follow_up_proposal(route)
            or record.plan.assignment.round_number != route.decision.round_number + 1
        ):
            raise TaskFactConflict("follow_up_source_invalid", "下一轮计划与图内补查路由不一致")


class FollowUpPlanService(_ReviewRoutePlanService):
    """保留首轮路由到第二轮计划的既有公开契约。"""

    async def plan(self, first_round: FirstRoundRoutingResult) -> CoordinationPlanRecord | None:
        return await self._plan_route(first_round.review_route)

    async def get_completed(
        self, first_round: FirstRoundRoutingResult
    ) -> CoordinationPlanRecord | None:
        return await self._get_completed_route(first_round.review_route)


class ThirdRoundPlanService(_ReviewRoutePlanService):
    """只消费第二轮审查路由并编译可选第三轮计划。"""

    @staticmethod
    def _route(result: SecondRoundRoutingResult) -> ReviewRoutingRecord:
        route = result.follow_up_review_route
        if result.review_route.decision.route != "follow_up" or route is None:
            raise TaskFactConflict("follow_up_source_invalid", "第三轮计划缺少第二轮审查路由")
        return route

    async def plan(self, result: SecondRoundRoutingResult) -> CoordinationPlanRecord | None:
        return await self._plan_route(self._route(result))

    async def get_completed(
        self, result: SecondRoundRoutingResult
    ) -> CoordinationPlanRecord | None:
        return await self._get_completed_route(self._route(result))
