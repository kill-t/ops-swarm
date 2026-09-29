"""在共享预算内按轮综合已提交材料并保存待审查开放假设。"""

import json
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.utils.function_calling import convert_to_openai_tool
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.investigation.budget.contracts import CallOperation
from opsswarm.investigation.budget.model_caller import BudgetedModelCaller
from opsswarm.investigation.budget.model_contracts import (
    ModelBudgetPolicy,
    ModelInput,
    TextModelMessage,
)
from opsswarm.investigation.budget.runtime import BudgetedCallRunner
from opsswarm.investigation.budget.tables import CallReservationRow
from opsswarm.investigation.context.contracts import ContextManifest
from opsswarm.investigation.context.prompts import OPEN_HYPOTHESIS_PROMPT
from opsswarm.investigation.context.service import TaskContextService
from opsswarm.investigation.dynamic_contracts import (
    FirstRoundInvestigationResult,
    FollowUpExecutionResult,
)
from opsswarm.investigation.models import InvestigationRequest
from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.investigation.synthesis.contracts import (
    OpenHypothesisProposal,
    OpenHypothesisRecord,
    SynthesisModelResult,
    SynthesisProposalCall,
)
from opsswarm.investigation.synthesis.persistence import (
    OpenHypothesisStore,
    validated_synthesis_call,
)
from opsswarm.investigation.tasks.errors import TaskFactConflict
from opsswarm.investigation.tasks.results import SpecialistTaskResult


def utc_now() -> datetime:
    return datetime.now(UTC)


class _RoundSynthesizer:
    """按显式轮次综合材料，并复用共享预算与开放假设存储。"""

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        *,
        run_id: UUID,
        lease_token: UUID,
        model: BaseChatModel,
        policy: ModelBudgetPolicy,
        runner: BudgetedCallRunner,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._factory = factory
        self._run_id = run_id
        self._lease_token = lease_token
        self._model = model
        self._policy = policy
        self._runner = runner
        self._store = OpenHypothesisStore(factory, clock=clock)

    async def _synthesize_first_round(
        self,
        request: InvestigationRequest,
        first_round: FirstRoundInvestigationResult,
    ) -> OpenHypothesisRecord:
        return await self._synthesize_round(
            request,
            round_id=first_round.coordination_plan.round_id,
            round_number=1,
            specialist_results=first_round.specialist_results,
        )

    async def _synthesize_round(
        self,
        request: InvestigationRequest,
        *,
        round_id: UUID,
        round_number: int,
        specialist_results: tuple[SpecialistTaskResult, ...],
    ) -> OpenHypothesisRecord:
        existing = await self._store.get_existing(
            self._run_id, round_id, lease_token=self._lease_token
        )
        if existing is not None:
            return existing
        supplied, messages = self._supplied_input(request, specialist_results)
        packet = await TaskContextService(self._factory, self._run_id).prepare(
            supplied,
            CallOperation.SYNTHESIZE,
            None,
            self._policy,
            round_number - 1,
        )
        if not isinstance(packet.model_input, ModelInput):
            raise TaskFactConflict(
                "dynamic_synthesis_context_invalid", "动态综合上下文返回了错误输入类型"
            )
        called = await self._reusable_call(
            packet.model_input,
            packet.manifest,
            invocation_index=round_number - 1,
        )
        if called is None:
            call_ids: list[UUID] = []
            proposal = await BudgetedModelCaller(self._model, self._policy, self._runner).invoke(
                OpenHypothesisProposal,
                messages,
                CallOperation.SYNTHESIZE,
                prepared_packet=packet,
                result_factory=lambda value: SynthesisModelResult(proposal=value),
                reservation_observer=call_ids.append,
                invocation_index=round_number - 1,
            )
            if len(call_ids) != 1:
                raise TaskFactConflict(
                    "dynamic_synthesis_call_invalid", "动态综合没有生成唯一预留身份"
                )
            called = SynthesisProposalCall(call_id=call_ids[0], proposal=proposal, reused=False)
        record, _ = await self._store.save(
            self._run_id,
            round_id,
            lease_token=self._lease_token,
            proposal=called.proposal,
            source_call_id=called.call_id,
        )
        return record

    async def _get_completed_first_round(
        self, first_round: FirstRoundInvestigationResult
    ) -> OpenHypothesisRecord:
        record = await self._store.get_existing(
            self._run_id,
            first_round.coordination_plan.round_id,
            lease_token=self._lease_token,
        )
        if record is None:
            raise TaskFactConflict("open_hypothesis_missing", "动态首轮尚未保存开放假设")
        return record

    def _supplied_input(
        self,
        request: InvestigationRequest,
        specialist_results: tuple[SpecialistTaskResult, ...],
    ) -> tuple[ModelInput, list[SystemMessage | HumanMessage]]:
        evidence = sorted(
            (item for result in specialist_results for item in result.evidence),
            key=lambda item: item.evidence_id,
        )
        reports = sorted(
            (result.report for result in specialist_results),
            key=lambda item: item.specialist.value,
        )
        payload = {
            "incident": request.model_dump(mode="json"),
            "evidence": [item.model_dump(mode="json") for item in evidence],
            "reports": [item.model_dump(mode="json") for item in reports],
        }
        messages: list[SystemMessage | HumanMessage] = [
            SystemMessage(content=OPEN_HYPOTHESIS_PROMPT),
            HumanMessage(
                content=json.dumps(
                    payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                )
            ),
        ]
        supplied = ModelInput(
            messages=tuple(
                TextModelMessage(
                    role="system" if isinstance(message, SystemMessage) else "user",
                    content=str(message.content),
                )
                for message in messages
            ),
            output_schema=OpenHypothesisProposal.model_json_schema(),
            tool_schema=convert_to_openai_tool(OpenHypothesisProposal),
            structured_output_method="function_calling",
        )
        return supplied, messages

    async def _reusable_call(
        self,
        model_input: ModelInput,
        manifest: ContextManifest,
        *,
        invocation_index: int,
    ) -> SynthesisProposalCall | None:
        async with self._factory() as session:
            run = await session.get(InvestigationRunRow, self._run_id)
            if run is None:
                raise TaskFactConflict("dynamic_synthesis_call_invalid", "动态综合运行不存在")
            rows = list(
                await session.scalars(
                    select(CallReservationRow)
                    .where(
                        CallReservationRow.investigation_run_id == self._run_id,
                        CallReservationRow.operation == CallOperation.SYNTHESIZE.value,
                        CallReservationRow.status == "succeeded",
                        CallReservationRow.invocation_index == invocation_index,
                    )
                    .order_by(CallReservationRow.sequence)
                )
            )
            if len(rows) > 1:
                raise TaskFactConflict(
                    "dynamic_synthesis_call_conflict", "当前轮次存在多个成功综合调用"
                )
            for row in rows:
                saved_input, result, saved_manifest = await validated_synthesis_call(
                    session, run, row.id
                )
                if saved_input == model_input and saved_manifest == manifest:
                    return SynthesisProposalCall(
                        call_id=row.id, proposal=result.proposal, reused=True
                    )
            if rows:
                raise TaskFactConflict(
                    "dynamic_synthesis_call_conflict", "当前轮次已有使用其他材料完成的综合调用"
                )
            return None


class FirstRoundSynthesizer(_RoundSynthesizer):
    """模型只提出首轮开放假设，持久化层注入待审查状态。"""

    async def synthesize(
        self,
        request: InvestigationRequest,
        first_round: FirstRoundInvestigationResult,
    ) -> OpenHypothesisRecord:
        return await self._synthesize_first_round(request, first_round)

    async def get_completed(
        self, first_round: FirstRoundInvestigationResult
    ) -> OpenHypothesisRecord:
        return await self._get_completed_first_round(first_round)


class SecondRoundSynthesizer(_RoundSynthesizer):
    """只投影第二轮补查结果，并保存与第二轮计划绑定的新开放假设。"""

    async def synthesize(
        self,
        request: InvestigationRequest,
        execution: FollowUpExecutionResult,
    ) -> OpenHypothesisRecord:
        plan = execution.follow_up_plan
        if (
            execution.review_route.decision.route != "follow_up"
            or plan is None
            or plan.plan.assignment.round_number != 2
        ):
            raise TaskFactConflict(
                "follow_up_synthesis_scope_invalid", "第二轮综合缺少已完成的补查计划"
            )
        return await self._synthesize_round(
            request,
            round_id=plan.round_id,
            round_number=2,
            specialist_results=execution.follow_up_results,
        )

    async def get_completed(self, execution: FollowUpExecutionResult) -> OpenHypothesisRecord:
        plan = execution.follow_up_plan
        if (
            execution.review_route.decision.route != "follow_up"
            or plan is None
            or plan.plan.assignment.round_number != 2
        ):
            raise TaskFactConflict(
                "follow_up_synthesis_scope_invalid", "第二轮综合缺少已完成的补查计划"
            )
        record = await self._store.get_existing(
            self._run_id,
            plan.round_id,
            lease_token=self._lease_token,
        )
        if record is None:
            raise TaskFactConflict("open_hypothesis_missing", "第二轮尚未保存开放假设")
        return record
