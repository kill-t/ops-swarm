"""在共享预算内调用协调模型，并将成功结果绑定到首轮计划。"""

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.incidents.persistence.tables import IncidentRow
from opsswarm.investigation.budget.contracts import CallOperation
from opsswarm.investigation.budget.model_caller import BudgetedModelCaller
from opsswarm.investigation.budget.model_contracts import ModelBudgetPolicy
from opsswarm.investigation.budget.runtime import BudgetedCallRunner
from opsswarm.investigation.budget.store import CallBudgetStore
from opsswarm.investigation.budget.tables import CallReservationRow
from opsswarm.investigation.context.contracts import ContextManifest
from opsswarm.investigation.coordination.context import CoordinationContextService
from opsswarm.investigation.coordination.contracts import (
    CoordinationModelInput,
    CoordinationModelResult,
    CoordinationPlanRecord,
    CoordinationPolicy,
    CoordinationProposal,
    CoordinationProposalCall,
)
from opsswarm.investigation.coordination.persistence import (
    CoordinationPlanStore,
    validated_coordination_call,
)
from opsswarm.investigation.coordination.policy import compile_coordination_plan
from opsswarm.investigation.coordination.queries import CoordinationPlanQueries
from opsswarm.investigation.errors import InvestigationRunNotFound
from opsswarm.investigation.models import InvestigationRequest
from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.investigation.tasks.errors import TaskFactConflict


def utc_now() -> datetime:
    return datetime.now(UTC)


class CoordinationPlanner:
    """首版只创建第一轮；后续轮次由动态图与摘要切片接入。"""

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        *,
        run_id: UUID,
        lease_token: UUID,
        model: BaseChatModel,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._factory = factory
        self._run_id = run_id
        self._lease_token = lease_token
        self._model = model
        self._plan_store = CoordinationPlanStore(factory, clock=clock)
        self._call_store = CallBudgetStore(factory, clock=clock)

    async def create_first_plan(
        self,
        *,
        policy: CoordinationPolicy,
        observation_cutoff_at: datetime,
        evidence_snapshot_id: str,
    ) -> tuple[CoordinationPlanRecord, bool, bool]:
        """返回计划、是否新建及是否复用了之前成功的模型调用。"""

        await self._plan_store.fix_model_policy(
            self._run_id, lease_token=self._lease_token, policy=policy
        )
        existing = await self._existing_first_plan(
            observation_cutoff_at=observation_cutoff_at,
            evidence_snapshot_id=evidence_snapshot_id,
        )
        if existing is not None:
            return existing, False, True
        called = await self._propose_after_policy_fixed(
            policy=policy,
            observation_cutoff_at=observation_cutoff_at,
            evidence_snapshot_id=evidence_snapshot_id,
        )
        plan, created = await self._plan_store.create_plan(
            self._run_id,
            lease_token=self._lease_token,
            policy=policy,
            proposal=called.proposal,
            observation_cutoff_at=observation_cutoff_at,
            evidence_snapshot_id=evidence_snapshot_id,
            source_call_id=called.call_id,
        )
        return plan, created, called.reused

    async def propose_first_round(
        self,
        *,
        policy: CoordinationPolicy,
        observation_cutoff_at: datetime,
        evidence_snapshot_id: str,
    ) -> CoordinationProposalCall:
        await self._plan_store.fix_model_policy(
            self._run_id, lease_token=self._lease_token, policy=policy
        )
        return await self._propose_after_policy_fixed(
            policy=policy,
            observation_cutoff_at=observation_cutoff_at,
            evidence_snapshot_id=evidence_snapshot_id,
        )

    async def _propose_after_policy_fixed(
        self,
        *,
        policy: CoordinationPolicy,
        observation_cutoff_at: datetime,
        evidence_snapshot_id: str,
    ) -> CoordinationProposalCall:
        packet = await CoordinationContextService(self._factory, self._run_id).prepare(
            observation_cutoff_at=observation_cutoff_at,
            evidence_snapshot_id=evidence_snapshot_id,
        )
        if not isinstance(packet.model_input, CoordinationModelInput):
            raise TaskFactConflict(
                "coordination_context_integrity_violation", "协调上下文返回了错误输入类型"
            )
        coordination_input = packet.model_input
        reused = await self._reusable_call(coordination_input, packet.manifest)
        if reused is not None:
            return reused
        model_policy = await self._model_policy()
        request = await self._request()
        runner = BudgetedCallRunner(
            run_id=self._run_id,
            lease_token=self._lease_token,
            store=self._call_store,
        )
        call_ids: list[UUID] = []
        messages = [
            SystemMessage(content=message.content)
            if message.role == "system"
            else HumanMessage(content=message.content)
            for message in coordination_input.messages
        ]

        def validated_result(value: CoordinationProposal) -> CoordinationModelResult:
            compile_coordination_plan(
                value,
                policy=policy,
                request=request,
                expected_round_number=coordination_input.round_number,
                existing_task_count=coordination_input.existing_task_count,
                observation_cutoff_at=coordination_input.observation_cutoff_at,
                evidence_snapshot_id=coordination_input.evidence_snapshot_id,
            )
            return CoordinationModelResult(proposal=value)

        proposal = await BudgetedModelCaller(self._model, model_policy, runner).invoke(
            CoordinationProposal,
            messages,
            CallOperation.COORDINATE,
            prepared_packet=packet,
            result_factory=validated_result,
            reservation_observer=call_ids.append,
        )
        if len(call_ids) != 1:
            raise TaskFactConflict(
                "coordination_call_integrity_violation", "协调调用没有生成唯一预留身份"
            )
        return CoordinationProposalCall(call_id=call_ids[0], proposal=proposal, reused=False)

    async def _existing_first_plan(
        self,
        *,
        observation_cutoff_at: datetime,
        evidence_snapshot_id: str,
    ) -> CoordinationPlanRecord | None:
        plans = await CoordinationPlanQueries(self._factory).list_plans(self._run_id)
        if not plans:
            return None
        plan = plans[0]
        tasks = plan.plan.assignment.tasks
        if (
            plan.plan.assignment.round_number != 1
            or plan.source_call_id is None
            or plan.source_review_route_id is not None
            or any(
                task.window_end != observation_cutoff_at
                or task.evidence_snapshot_id != evidence_snapshot_id
                for task in tasks
            )
        ):
            raise TaskFactConflict(
                "coordination_plan_conflict", "已提交首轮计划与本次恢复参数不一致"
            )
        return plan

    async def _model_policy(self) -> ModelBudgetPolicy:
        async with self._factory() as session:
            run = await session.get(InvestigationRunRow, self._run_id)
            if run is None:
                raise InvestigationRunNotFound(self._run_id)
            if run.model_budget is None:
                raise TaskFactConflict("coordination_context_missing", "协调调用缺少固定模型预算")
            return ModelBudgetPolicy.model_validate(run.model_budget)

    async def _request(self) -> InvestigationRequest:
        async with self._factory() as session:
            run = await session.get(InvestigationRunRow, self._run_id)
            if run is None:
                raise InvestigationRunNotFound(self._run_id)
            incident = await session.get(IncidentRow, run.incident_id)
            if incident is None:
                raise TaskFactConflict("coordination_context_missing", "协调调用缺少事故来源")
            return InvestigationRequest(
                incident_id=incident.id,
                service_name=incident.service_name,
                title=incident.title,
                started_at=incident.started_at,
            )

    async def _reusable_call(
        self, model_input: CoordinationModelInput, manifest: ContextManifest
    ) -> CoordinationProposalCall | None:
        async with self._factory() as session:
            run = await session.get(InvestigationRunRow, self._run_id)
            if run is None:
                raise InvestigationRunNotFound(self._run_id)
            rows = list(
                await session.scalars(
                    select(CallReservationRow)
                    .where(
                        CallReservationRow.investigation_run_id == self._run_id,
                        CallReservationRow.operation == CallOperation.COORDINATE.value,
                        CallReservationRow.status == "succeeded",
                    )
                    .order_by(CallReservationRow.sequence)
                )
            )
            if len(rows) > 1:
                raise TaskFactConflict(
                    "coordination_call_conflict", "该运行存在多个成功的首轮协调调用"
                )
            for row in rows:
                saved_input, result, _ = await validated_coordination_call(session, run, row.id)
                saved_manifest = ContextManifest.model_validate(row.context_manifest)
                if saved_input == model_input and saved_manifest == manifest:
                    return CoordinationProposalCall(
                        call_id=row.id, proposal=result.proposal, reused=True
                    )
            if rows:
                raise TaskFactConflict(
                    "coordination_call_conflict",
                    "该运行已有使用其他上下文完成的协调调用",
                )
            return None
