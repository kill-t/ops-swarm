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
from opsswarm.investigation.prompt_versions import UNTRUSTED_EVIDENCE_POLICY
from opsswarm.investigation.settings import StructuredOutputMethod


class LangChainSpecialistAnalyst:
    """使用同一模型适配器生成不同专业的结构化报告。"""

    def __init__(
        self,
        model: BaseChatModel,
        *,
        structured_output_method: StructuredOutputMethod = "json_schema",
    ) -> None:
        self._structured_output_method = structured_output_method
        self._structured_model = model.with_structured_output(
            SpecialistReport,
            method=structured_output_method,
        )

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
                    content=self._system_prompt(
                        "你是 OpsSwarm 的专业调查节点。只分析分配给你的证据，不补充未出现的事实；"
                        "evidence_ids 只能引用输入证据，输出必须符合结构化报告。"
                        f"{UNTRUSTED_EVIDENCE_POLICY}"
                    )
                ),
                HumanMessage(
                    content=json.dumps(
                        self._payload(
                            {
                                "specialist": specialist.value,
                                "incident": request.model_dump(mode="json"),
                                "evidence": [item.model_dump(mode="json") for item in evidence],
                            },
                            SpecialistReport,
                        ),
                        ensure_ascii=False,
                    )
                ),
            ]
        )
        return SpecialistReport.model_validate(response)

    def _system_prompt(self, prompt: str) -> str:
        if self._structured_output_method != "json_mode":
            return prompt
        return f"{prompt} 必须只输出符合下方 JSON Schema 的 JSON 对象。"

    def _payload(
        self, payload: dict[str, object], schema: type[SpecialistReport]
    ) -> dict[str, object]:
        if self._structured_output_method == "json_mode":
            payload["output_json_schema"] = schema.model_json_schema()
        return payload


class LangChainConclusionSynthesizer:
    """综合多份专业报告，但没有任何执行工具。"""

    def __init__(
        self,
        model: BaseChatModel,
        *,
        structured_output_method: StructuredOutputMethod = "json_schema",
    ) -> None:
        self._structured_output_method = structured_output_method
        self._structured_model = model.with_structured_output(
            InvestigationConclusion,
            method=structured_output_method,
        )

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
                    content=self._system_prompt(
                        "你是 OpsSwarm 的综合调查 Agent。比较日志、指标和变更报告，给出最可能根因；"
                        "证据不足时选择 undetermined。"
                        "回滚只能作为需要人工审批的建议，禁止声称已执行。"
                        f"{UNTRUSTED_EVIDENCE_POLICY}"
                        "建议必须按根因选择：版本回归用 rollback_release；配置错误用 "
                        "rollback_configuration；依赖不可用用 restore_dependency；证据不足或"
                        "没有明确受控动作时用 continue_investigation。"
                    )
                ),
                HumanMessage(
                    content=json.dumps(
                        self._payload(
                            {
                                "incident": request.model_dump(mode="json"),
                                "evidence": [item.model_dump(mode="json") for item in evidence],
                                "reports": [item.model_dump(mode="json") for item in reports],
                            },
                        ),
                        ensure_ascii=False,
                    )
                ),
            ]
        )
        return InvestigationConclusion.model_validate(response)

    def _system_prompt(self, prompt: str) -> str:
        if self._structured_output_method != "json_mode":
            return prompt
        return f"{prompt} 必须只输出符合下方 JSON Schema 的 JSON 对象。"

    def _payload(self, payload: dict[str, object]) -> dict[str, object]:
        if self._structured_output_method == "json_mode":
            payload["output_json_schema"] = InvestigationConclusion.model_json_schema()
        return payload
