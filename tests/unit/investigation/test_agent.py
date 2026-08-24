from collections.abc import Callable, Sequence
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langchain_core.runnables import Runnable

from opsswarm.investigation.agent import SingleAgentInvestigator
from opsswarm.investigation.errors import UnverifiedEvidenceReference
from opsswarm.investigation.evaluation import evaluate_investigation
from opsswarm.investigation.models import Recommendation, RootCause
from opsswarm.investigation.scenarios import (
    RELEASE_500_SCENARIO,
    FrozenScenarioDataSource,
)


class ToolCallingFakeModel(FakeMessagesListChatModel):
    """LangChain 自带假模型的工具绑定测试适配，不发起网络请求。"""

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any]],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Runnable[Any, AIMessage]:
        del tools, tool_choice, kwargs
        return self


def build_fake_model(*, evidence_ids: list[str]) -> ToolCallingFakeModel:
    """先并行查询三类证据，再返回一次结构化结论。"""

    return ToolCallingFakeModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "query_service_logs",
                        "args": {"service_name": "order-service", "lookback_minutes": 15},
                        "id": "call-logs",
                        "type": "tool_call",
                    },
                    {
                        "name": "query_service_metrics",
                        "args": {"service_name": "order-service", "lookback_minutes": 15},
                        "id": "call-metrics",
                        "type": "tool_call",
                    },
                    {
                        "name": "list_recent_changes",
                        "args": {"service_name": "order-service", "lookback_hours": 6},
                        "id": "call-changes",
                        "type": "tool_call",
                    },
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "InvestigationConclusion",
                        "args": {
                            "root_cause": "release_500_regression",
                            "confidence": 0.96,
                            "summary": "新版本发布与 5xx 激增时间一致，日志指向该版本运行时异常。",
                            "evidence_ids": evidence_ids,
                            "recommendation": "rollback_release",
                            "requires_human_approval": True,
                            "safety_notes": ["只能由白名单执行器在审批后回滚。"],
                        },
                        "id": "call-structured-conclusion",
                        "type": "tool_call",
                    }
                ],
            ),
        ]
    )


@pytest.mark.asyncio
async def test_single_agent_collects_evidence_and_returns_structured_conclusion() -> None:
    scenario = RELEASE_500_SCENARIO
    investigator = SingleAgentInvestigator(
        model=build_fake_model(evidence_ids=sorted(scenario.required_evidence_ids)),
        data_source=FrozenScenarioDataSource(scenario),
    )

    result = await investigator.investigate(scenario.request)
    metrics = evaluate_investigation(result, scenario)

    assert result.conclusion.root_cause is RootCause.RELEASE_500_REGRESSION
    assert result.conclusion.recommendation is Recommendation.ROLLBACK_RELEASE
    assert result.conclusion.requires_human_approval is True
    assert set(result.returned_evidence_ids) == scenario.required_evidence_ids
    assert result.tool_call_count == 3
    assert metrics.top1_accuracy == 1
    assert metrics.evidence_recall == 1
    assert metrics.recommendation_accuracy == 1
    assert metrics.safety_violation_rate == 0


@pytest.mark.asyncio
async def test_single_agent_rejects_evidence_not_returned_by_tools() -> None:
    scenario = RELEASE_500_SCENARIO
    investigator = SingleAgentInvestigator(
        model=build_fake_model(evidence_ids=["invented-evidence"]),
        data_source=FrozenScenarioDataSource(scenario),
    )

    with pytest.raises(UnverifiedEvidenceReference) as error:
        await investigator.investigate(scenario.request)

    assert error.value.evidence_ids == frozenset({"invented-evidence"})
