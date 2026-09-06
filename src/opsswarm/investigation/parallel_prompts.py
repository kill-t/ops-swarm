"""固定专业与综合节点的文本提示组装，保持历史提示契约。"""

import json
from collections.abc import Sequence

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from pydantic import BaseModel

from opsswarm.investigation.models import (
    EvidenceKind,
    EvidenceObservation,
    InvestigationConclusion,
    InvestigationRequest,
    SpecialistReport,
)
from opsswarm.investigation.prompt_versions import UNTRUSTED_EVIDENCE_POLICY
from opsswarm.investigation.settings import StructuredOutputMethod


def _messages(
    prompt: str, payload: dict[str, object], schema: type[BaseModel], method: StructuredOutputMethod
) -> list[BaseMessage]:
    if method == "json_mode":
        prompt += " 必须只输出符合下方 JSON Schema 的 JSON 对象。"
        payload["output_json_schema"] = schema.model_json_schema()
    return [
        SystemMessage(content=prompt),
        HumanMessage(content=json.dumps(payload, ensure_ascii=False)),
    ]


def specialist_messages(
    specialist: EvidenceKind,
    request: InvestigationRequest,
    evidence: Sequence[EvidenceObservation],
    method: StructuredOutputMethod,
) -> list[BaseMessage]:
    return _messages(
        "你是 OpsSwarm 的专业调查节点。只分析分配给你的证据，不补充未出现的事实；"
        "evidence_ids 只能引用输入证据，输出必须符合结构化报告。"
        f"{UNTRUSTED_EVIDENCE_POLICY}",
        {
            "specialist": specialist.value,
            "incident": request.model_dump(mode="json"),
            "evidence": [item.model_dump(mode="json") for item in evidence],
        },
        SpecialistReport,
        method,
    )


def conclusion_messages(
    request: InvestigationRequest,
    evidence: Sequence[EvidenceObservation],
    reports: Sequence[SpecialistReport],
    method: StructuredOutputMethod,
) -> list[BaseMessage]:
    return _messages(
        "你是 OpsSwarm 的综合调查 Agent。比较日志、指标和变更报告，给出最可能根因；"
        "证据不足时选择 undetermined。"
        "回滚只能作为需要人工审批的建议，禁止声称已执行。"
        f"{UNTRUSTED_EVIDENCE_POLICY}"
        "建议必须按根因选择：版本回归用 rollback_release；配置错误用 "
        "rollback_configuration；依赖不可用用 restore_dependency；证据不足或"
        "没有明确受控动作时用 continue_investigation。",
        {
            "incident": request.model_dump(mode="json"),
            "evidence": [item.model_dump(mode="json") for item in evidence],
            "reports": [item.model_dump(mode="json") for item in reports],
        },
        InvestigationConclusion,
        method,
    )
