"""单 Agent、多 Agent 与持久模式路由处理器。"""

from typing import Protocol

from langchain_core.language_models.chat_models import BaseChatModel

from opsswarm.investigation.agent import SingleAgentInvestigator
from opsswarm.investigation.evaluation import evaluate_investigation
from opsswarm.investigation.models import (
    InvestigationMode,
    InvestigationRequest,
    MultiAgentInvestigationResult,
    WorkItemKind,
)
from opsswarm.investigation.scenarios import RELEASE_500_SCENARIO, FrozenScenarioDataSource
from opsswarm.worker.runner import InvestigationProcessor, ProcessorContext, ProcessorOutcome


class CheckpointedParallelInvestigator(Protocol):
    """多 Agent Worker 所需的最小图能力。"""

    async def has_checkpoint(self, *, thread_id: str) -> bool:
        """线程是否已经拥有恢复状态。"""

    async def investigate(
        self,
        request: InvestigationRequest,
        *,
        thread_id: str | None = None,
    ) -> MultiAgentInvestigationResult:
        """首次运行调查图。"""

    async def resume(self, *, thread_id: str) -> MultiAgentInvestigationResult:
        """从已有线程恢复调查图。"""


class LangChainBaselineProcessor:
    """使用冻结演练数据运行真实可注入模型；不连接生产遥测。"""

    def __init__(self, *, model: BaseChatModel, model_name: str) -> None:
        self._investigator = SingleAgentInvestigator(
            model=model,
            data_source=FrozenScenarioDataSource(RELEASE_500_SCENARIO),
        )
        self._model_name = model_name

    async def process(self, context: ProcessorContext) -> ProcessorOutcome:
        _require_route(
            context,
            mode=InvestigationMode.SINGLE_AGENT,
            kind=WorkItemKind.RUN_SINGLE_AGENT_INVESTIGATION,
        )
        artifacts = await self._investigator.investigate_with_evidence(context.request)
        return ProcessorOutcome(
            result=artifacts.result,
            evidence_items=artifacts.evidence_items,
            metrics=evaluate_investigation(artifacts.result, RELEASE_500_SCENARIO),
            model_name=self._model_name,
        )


class LangGraphMultiAgentProcessor:
    """根据 checkpoint 是否存在确定性选择首次运行或恢复。"""

    def __init__(
        self,
        *,
        investigator: CheckpointedParallelInvestigator,
        model_name: str,
    ) -> None:
        self._investigator = investigator
        self._model_name = model_name

    async def process(self, context: ProcessorContext) -> ProcessorOutcome:
        _require_route(
            context,
            mode=InvestigationMode.MULTI_AGENT,
            kind=WorkItemKind.RUN_MULTI_AGENT_INVESTIGATION,
        )
        thread_id = context.graph_thread_id
        if thread_id is None:
            raise RuntimeError("多 Agent 调查缺少 graph_thread_id")
        if await self._investigator.has_checkpoint(thread_id=thread_id):
            outcome = await self._investigator.resume(thread_id=thread_id)
        else:
            outcome = await self._investigator.investigate(
                context.request,
                thread_id=thread_id,
            )
        result = outcome.result
        return ProcessorOutcome(
            result=result,
            evidence_items=outcome.evidence_items,
            specialist_reports=outcome.specialist_reports,
            metrics=evaluate_investigation(result, RELEASE_500_SCENARIO),
            model_name=self._model_name,
        )


class ModeRoutingProcessor:
    """只依据持久化模式选择处理器，不让模型参与任务路由。"""

    def __init__(
        self,
        *,
        single_agent: InvestigationProcessor,
        multi_agent: InvestigationProcessor,
    ) -> None:
        self._processors: dict[InvestigationMode, InvestigationProcessor] = {
            InvestigationMode.SINGLE_AGENT: single_agent,
            InvestigationMode.MULTI_AGENT: multi_agent,
        }

    async def process(self, context: ProcessorContext) -> ProcessorOutcome:
        return await self._processors[context.mode].process(context)


def _require_route(
    context: ProcessorContext,
    *,
    mode: InvestigationMode,
    kind: WorkItemKind,
) -> None:
    if context.mode is not mode or context.work_item_kind is not kind:
        raise RuntimeError("调查运行模式与持久任务类型不匹配")
