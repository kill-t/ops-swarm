"""专业节点与综合节点的结构化模型适配。"""

from collections.abc import Sequence

from langchain_core.language_models.chat_models import BaseChatModel

from opsswarm.investigation.budget.contracts import CallOperation
from opsswarm.investigation.budget.model_caller import BudgetedModelCaller
from opsswarm.investigation.models import (
    EvidenceKind,
    EvidenceObservation,
    InvestigationConclusion,
    InvestigationRequest,
    SpecialistReport,
)
from opsswarm.investigation.parallel_prompts import conclusion_messages, specialist_messages
from opsswarm.investigation.settings import StructuredOutputMethod


class LangChainSpecialistAnalyst:
    """只分析分配给本专业的证据，由可选预算入口调用模型。"""

    def __init__(
        self,
        model: BaseChatModel,
        *,
        structured_output_method: StructuredOutputMethod = "json_schema",
        model_caller: BudgetedModelCaller | None = None,
    ) -> None:
        self._method = structured_output_method
        self._caller = model_caller
        self._structured_model = (
            model.with_structured_output(SpecialistReport, method=structured_output_method)
            if model_caller is None
            else None
        )

    async def analyze(
        self,
        *,
        specialist: EvidenceKind,
        request: InvestigationRequest,
        evidence: Sequence[EvidenceObservation],
    ) -> SpecialistReport:
        messages = specialist_messages(specialist, request, evidence, self._method)
        if self._caller is not None:
            return await self._caller.invoke(
                SpecialistReport, messages, CallOperation(f"analyze_{specialist.value}")
            )
        assert self._structured_model is not None
        return SpecialistReport.model_validate(await self._structured_model.ainvoke(messages))


class LangChainConclusionSynthesizer:
    """综合专业报告，保持独立输出契约且不持有执行工具。"""

    def __init__(
        self,
        model: BaseChatModel,
        *,
        structured_output_method: StructuredOutputMethod = "json_schema",
        model_caller: BudgetedModelCaller | None = None,
    ) -> None:
        self._method = structured_output_method
        self._caller = model_caller
        self._structured_model = (
            model.with_structured_output(InvestigationConclusion, method=structured_output_method)
            if model_caller is None
            else None
        )

    async def synthesize(
        self,
        *,
        request: InvestigationRequest,
        evidence: Sequence[EvidenceObservation],
        reports: Sequence[SpecialistReport],
    ) -> InvestigationConclusion:
        messages = conclusion_messages(request, evidence, reports, self._method)
        if self._caller is not None:
            return await self._caller.invoke(
                InvestigationConclusion, messages, CallOperation.SYNTHESIZE
            )
        assert self._structured_model is not None
        return InvestigationConclusion.model_validate(
            await self._structured_model.ainvoke(messages)
        )
