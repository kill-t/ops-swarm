"""LangGraph 专业节点与综合节点的 LangChain 模型适配。"""

import json
from collections.abc import Sequence

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from opsswarm.investigation.models import (
    EvidenceKind,
    EvidenceObservation,
    InvestigationConclusion,
    InvestigationRequest,
    SpecialistReport,
)


class LangChainSpecialistAnalyst:
    """使用同一模型适配器生成不同专业的结构化报告。"""

    def __init__(self, model: BaseChatModel) -> None:
        self._structured_model = model.with_structured_output(SpecialistReport)

    async def analyze(
        self,
        *,
        specialist: EvidenceKind,
        request: InvestigationRequest,
        evidence: Sequence[EvidenceObservation],
    ) -> SpecialistReport:
        response = await self._structured_model.ainvoke(
            [
                SystemMessage(
                    content=(
                        "你是 OpsSwarm 的专业调查节点。只分析分配给你的证据，不补充未出现的事实；"
                        "evidence_ids 只能引用输入证据，输出必须符合结构化报告。"
                    )
                ),
                HumanMessage(
                    content=json.dumps(
                        {
                            "specialist": specialist.value,
                            "incident": request.model_dump(mode="json"),
                            "evidence": [item.model_dump(mode="json") for item in evidence],
                        },
                        ensure_ascii=False,
                    )
                ),
            ]
        )
        return SpecialistReport.model_validate(response)


class LangChainConclusionSynthesizer:
    """综合多份专业报告，但没有任何执行工具。"""

    def __init__(self, model: BaseChatModel) -> None:
        self._structured_model = model.with_structured_output(InvestigationConclusion)

    async def synthesize(
        self,
        *,
        request: InvestigationRequest,
        evidence: Sequence[EvidenceObservation],
        reports: Sequence[SpecialistReport],
    ) -> InvestigationConclusion:
        response = await self._structured_model.ainvoke(
            [
                SystemMessage(
                    content=(
                        "你是 OpsSwarm 的综合调查 Agent。比较日志、指标和变更报告，给出最可能根因；"
                        "证据不足时选择 undetermined。"
                        "回滚只能作为需要人工审批的建议，禁止声称已执行。"
                    )
                ),
                HumanMessage(
                    content=json.dumps(
                        {
                            "incident": request.model_dump(mode="json"),
                            "evidence": [item.model_dump(mode="json") for item in evidence],
                            "reports": [item.model_dump(mode="json") for item in reports],
                        },
                        ensure_ascii=False,
                    )
                ),
            ]
        )
        return InvestigationConclusion.model_validate(response)
