"""在共享预算内综合首轮材料并提交待审查开放假设。"""

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
from opsswarm.investigation.dynamic_contracts import FirstRoundInvestigationResult
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


def utc_now() -> datetime:
    return datetime.now(UTC)


class FirstRoundSynthesizer:
    """模型只提出开放假设，持久化层注入待审查状态并复核来源。"""

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

    async def synthesize(
        self,
        request: InvestigationRequest,
        first_round: FirstRoundInvestigationResult,
    ) -> OpenHypothesisRecord:
        round_id = first_round.coordination_plan.round_id
        existing = await self._store.get_existing(
            self._run_id, round_id, lease_token=self._lease_token
        )
        if existing is not None:
            return existing
        supplied, messages = self._supplied_input(request, first_round)
        packet = await TaskContextService(self._factory, self._run_id).prepare(
            supplied,
            CallOperation.SYNTHESIZE,
            None,
            self._policy,
        )
        if not isinstance(packet.model_input, ModelInput):
            raise TaskFactConflict(
                "dynamic_synthesis_context_invalid", "动态综合上下文返回了错误输入类型"
            )
        called = await self._reusable_call(packet.model_input, packet.manifest)
        if called is None:
            call_ids: list[UUID] = []
            proposal = await BudgetedModelCaller(self._model, self._policy, self._runner).invoke(
                OpenHypothesisProposal,
                messages,
                CallOperation.SYNTHESIZE,
                prepared_packet=packet,
                result_factory=lambda value: SynthesisModelResult(proposal=value),
                reservation_observer=call_ids.append,
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

    async def get_completed(
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
        first_round: FirstRoundInvestigationResult,
    ) -> tuple[ModelInput, list[SystemMessage | HumanMessage]]:
        evidence = sorted(
            (item for result in first_round.specialist_results for item in result.evidence),
            key=lambda item: item.evidence_id,
        )
        reports = sorted(
            (result.report for result in first_round.specialist_results),
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
        self, model_input: ModelInput, manifest: ContextManifest
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
                    )
                    .order_by(CallReservationRow.sequence)
                )
            )
            if len(rows) > 1:
                raise TaskFactConflict(
                    "dynamic_synthesis_call_conflict", "该运行存在多个成功首轮综合调用"
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
                    "dynamic_synthesis_call_conflict", "该运行已有使用其他材料完成的综合调用"
                )
            return None
