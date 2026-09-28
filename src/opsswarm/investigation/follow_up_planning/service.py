"""把确定性补查路由提交为第二轮协调计划。"""

from collections.abc import Callable
from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.investigation.coordination.contracts import CoordinationPlanRecord
from opsswarm.investigation.coordination.persistence import CoordinationPlanStore, utc_now
from opsswarm.investigation.dynamic_contracts import FirstRoundRoutingResult
from opsswarm.investigation.follow_up_planning.policy import compile_follow_up_proposal
from opsswarm.investigation.tasks.errors import TaskFactConflict


class FollowUpPlanService:
    """不调用模型或工具，只消费已重建通过的路由事实。"""

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

    async def plan(self, first_round: FirstRoundRoutingResult) -> CoordinationPlanRecord | None:
        self._validate_result(first_round)
        route = first_round.review_route
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
        self._validate_record(record, first_round)
        return record

    async def get_completed(
        self, first_round: FirstRoundRoutingResult
    ) -> CoordinationPlanRecord | None:
        self._validate_result(first_round)
        route = first_round.review_route
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
            raise TaskFactConflict("follow_up_plan_missing", "补查路由尚未保存第二轮计划")
        self._validate_record(record, first_round)
        return record

    def _validate_result(self, first_round: FirstRoundRoutingResult) -> None:
        if first_round.review_route.investigation_run_id != self._run_id:
            raise TaskFactConflict("follow_up_source_invalid", "补查路由不属于当前运行")

    @staticmethod
    def _validate_record(
        record: CoordinationPlanRecord,
        first_round: FirstRoundRoutingResult,
    ) -> None:
        route = first_round.review_route
        if (
            record.investigation_run_id != route.investigation_run_id
            or record.source_review_route_id != route.id
            or record.source_call_id is not None
            or record.plan.proposal != compile_follow_up_proposal(route)
            or record.plan.assignment.round_number != route.decision.round_number + 1
        ):
            raise TaskFactConflict("follow_up_source_invalid", "第二轮计划与图内补查路由不一致")
