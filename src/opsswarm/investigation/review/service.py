"""在共享预算内按轮独立审查开放假设并提交不可变意见。"""

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
from opsswarm.investigation.context.prompts import (
    FOLLOW_UP_HYPOTHESIS_REVIEW_PROMPT,
    HYPOTHESIS_REVIEW_PROMPT,
    THIRD_ROUND_HYPOTHESIS_REVIEW_PROMPT,
)
from opsswarm.investigation.context.service import TaskContextService
from opsswarm.investigation.dynamic_contracts import (
    FirstRoundSynthesisResult,
    SecondRoundSynthesisResult,
    ThirdRoundSynthesisResult,
)
from opsswarm.investigation.models import InvestigationRequest
from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.investigation.review.contracts import (
    HypothesisReviewProposal,
    HypothesisReviewRecord,
    ReviewModelResult,
    ReviewProposalCall,
)
from opsswarm.investigation.review.persistence import (
    HypothesisReviewStore,
    validated_review_call,
)
from opsswarm.investigation.synthesis.contracts import OpenHypothesisRecord
from opsswarm.investigation.tasks.errors import TaskFactConflict
from opsswarm.investigation.tasks.results import SpecialistTaskResult


def utc_now() -> datetime:
    return datetime.now(UTC)


class _RoundHypothesisReviewer:
    """按显式业务轮次复用审查上下文、预算和事实存储。"""

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
        self._store = HypothesisReviewStore(factory, clock=clock)

    async def _review_round(
        self,
        request: InvestigationRequest,
        *,
        open_hypothesis: OpenHypothesisRecord,
        specialist_results: tuple[SpecialistTaskResult, ...],
        round_number: int,
    ) -> HypothesisReviewRecord:
        existing = await self._store.get_existing(
            self._run_id, open_hypothesis.id, lease_token=self._lease_token
        )
        if existing is not None:
            return existing
        supplied, messages = self._supplied_input(
            request,
            open_hypothesis=open_hypothesis,
            specialist_results=specialist_results,
            round_number=round_number,
        )
        invocation_index = round_number - 1
        packet = await TaskContextService(self._factory, self._run_id).prepare(
            supplied,
            CallOperation.REVIEW,
            None,
            self._policy,
            invocation_index,
        )
        if not isinstance(packet.model_input, ModelInput):
            raise TaskFactConflict(
                "hypothesis_review_context_invalid", "独立审查上下文返回了错误输入类型"
            )
        called = await self._reusable_call(
            packet.model_input,
            packet.manifest,
            invocation_index=invocation_index,
            expected_round_number=round_number,
        )
        if called is None:
            call_ids: list[UUID] = []
            proposal = await BudgetedModelCaller(self._model, self._policy, self._runner).invoke(
                HypothesisReviewProposal,
                messages,
                CallOperation.REVIEW,
                prepared_packet=packet,
                result_factory=lambda value: ReviewModelResult(proposal=value),
                reservation_observer=call_ids.append,
                invocation_index=invocation_index,
            )
            if len(call_ids) != 1:
                raise TaskFactConflict(
                    "hypothesis_review_call_invalid", "独立审查没有生成唯一预留身份"
                )
            called = ReviewProposalCall(call_id=call_ids[0], proposal=proposal, reused=False)
        record, _ = await self._store.save(
            self._run_id,
            open_hypothesis.id,
            lease_token=self._lease_token,
            proposal=called.proposal,
            source_call_id=called.call_id,
        )
        return record

    async def _get_completed(self, open_hypothesis: OpenHypothesisRecord) -> HypothesisReviewRecord:
        record = await self._store.get_existing(
            self._run_id,
            open_hypothesis.id,
            lease_token=self._lease_token,
        )
        if record is None:
            raise TaskFactConflict("hypothesis_review_missing", "开放假设尚未保存独立审查")
        return record

    @staticmethod
    def _supplied_input(
        request: InvestigationRequest,
        *,
        open_hypothesis: OpenHypothesisRecord,
        specialist_results: tuple[SpecialistTaskResult, ...],
        round_number: int,
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
            "open_hypothesis": open_hypothesis.model_dump(mode="json"),
        }
        if round_number == 1:
            prompt = HYPOTHESIS_REVIEW_PROMPT
        elif round_number == 2:
            prompt = FOLLOW_UP_HYPOTHESIS_REVIEW_PROMPT
        elif round_number == 3:
            prompt = THIRD_ROUND_HYPOTHESIS_REVIEW_PROMPT
        else:
            raise TaskFactConflict("hypothesis_review_scope_invalid", "独立审查轮次不受支持")
        messages: list[SystemMessage | HumanMessage] = [
            SystemMessage(content=prompt),
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
            output_schema=HypothesisReviewProposal.model_json_schema(),
            tool_schema=convert_to_openai_tool(HypothesisReviewProposal),
            structured_output_method="function_calling",
        )
        return supplied, messages

    async def _reusable_call(
        self,
        model_input: ModelInput,
        manifest: ContextManifest,
        *,
        invocation_index: int,
        expected_round_number: int,
    ) -> ReviewProposalCall | None:
        async with self._factory() as session:
            run = await session.get(InvestigationRunRow, self._run_id)
            if run is None:
                raise TaskFactConflict("hypothesis_review_call_invalid", "独立审查运行不存在")
            rows = list(
                await session.scalars(
                    select(CallReservationRow)
                    .where(
                        CallReservationRow.investigation_run_id == self._run_id,
                        CallReservationRow.operation == CallOperation.REVIEW.value,
                        CallReservationRow.status == "succeeded",
                        CallReservationRow.invocation_index == invocation_index,
                    )
                    .order_by(CallReservationRow.sequence)
                )
            )
            if len(rows) > 1:
                raise TaskFactConflict(
                    "hypothesis_review_call_conflict", "当前轮次存在多个成功独立审查调用"
                )
            for row in rows:
                saved_input, result, saved_manifest = await validated_review_call(
                    session,
                    run,
                    row.id,
                    expected_round_number=expected_round_number,
                )
                if saved_input == model_input and saved_manifest == manifest:
                    return ReviewProposalCall(
                        call_id=row.id,
                        proposal=result.proposal,
                        reused=True,
                    )
            if rows:
                raise TaskFactConflict(
                    "hypothesis_review_call_conflict",
                    "当前轮次已有使用其他材料完成的独立审查调用",
                )
            return None


class IndependentHypothesisReviewer(_RoundHypothesisReviewer):
    """只审查首轮开放假设，保留既有公开调用契约。"""

    async def review(
        self,
        request: InvestigationRequest,
        first_round: FirstRoundSynthesisResult,
    ) -> HypothesisReviewRecord:
        return await self._review_round(
            request,
            open_hypothesis=first_round.open_hypothesis,
            specialist_results=first_round.specialist_results,
            round_number=1,
        )

    async def get_completed(self, first_round: FirstRoundSynthesisResult) -> HypothesisReviewRecord:
        return await self._get_completed(first_round.open_hypothesis)


class SecondRoundHypothesisReviewer(_RoundHypothesisReviewer):
    """只审查第二轮补查材料形成的新开放假设。"""

    @staticmethod
    def _hypothesis(result: SecondRoundSynthesisResult) -> OpenHypothesisRecord:
        if (
            result.review_route.decision.route != "follow_up"
            or result.follow_up_plan is None
            or result.follow_up_hypothesis is None
            or result.follow_up_plan.plan.assignment.round_number != 2
        ):
            raise TaskFactConflict(
                "follow_up_review_scope_invalid", "第二轮审查缺少已完成的补查综合事实"
            )
        return result.follow_up_hypothesis

    async def review(
        self,
        request: InvestigationRequest,
        result: SecondRoundSynthesisResult,
    ) -> HypothesisReviewRecord:
        return await self._review_round(
            request,
            open_hypothesis=self._hypothesis(result),
            specialist_results=result.follow_up_results,
            round_number=2,
        )

    async def get_completed(self, result: SecondRoundSynthesisResult) -> HypothesisReviewRecord:
        return await self._get_completed(self._hypothesis(result))


class ThirdRoundHypothesisReviewer(_RoundHypothesisReviewer):
    """只审查第三轮补查材料形成的新开放假设。"""

    @staticmethod
    def _hypothesis(result: ThirdRoundSynthesisResult) -> OpenHypothesisRecord:
        if (
            result.third_round_plan is None
            or result.third_round_hypothesis is None
            or result.third_round_plan.plan.assignment.round_number != 3
        ):
            raise TaskFactConflict(
                "third_round_review_scope_invalid", "第三轮审查缺少已完成的第三轮综合事实"
            )
        return result.third_round_hypothesis

    async def review(
        self,
        request: InvestigationRequest,
        result: ThirdRoundSynthesisResult,
    ) -> HypothesisReviewRecord:
        return await self._review_round(
            request,
            open_hypothesis=self._hypothesis(result),
            specialist_results=result.third_round_results,
            round_number=3,
        )

    async def get_completed(self, result: ThirdRoundSynthesisResult) -> HypothesisReviewRecord:
        return await self._get_completed(self._hypothesis(result))
