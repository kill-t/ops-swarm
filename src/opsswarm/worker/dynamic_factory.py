"""为当前动态Worker按持久来源装配冻结或现场调查图。"""

from collections.abc import Callable
from datetime import datetime

from langchain_openai import ChatOpenAI
from langgraph.checkpoint.base import BaseCheckpointSaver
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.investigation.dynamic_third_round_routing_graph import DynamicThirdRoundRoutingGraph
from opsswarm.investigation.live import LiveInvestigationDataSource
from opsswarm.investigation.models import InvestigationDataSourceMode, InvestigationRequest
from opsswarm.investigation.query_execution import ReadOnlyQueryExecutor
from opsswarm.investigation.scenarios import RELEASE_500_SCENARIO, FrozenScenarioDataSource
from opsswarm.worker.dynamic_graph import DynamicThirdRoundRoutingGraphFactory
from opsswarm.worker.runner import ProcessorContext


def worker_frozen_source(request: InvestigationRequest) -> FrozenScenarioDataSource:
    """最新模拟观察锚定事故开始，保留相对时间与基线正文，不制造现场证据。"""

    if request.service_name != RELEASE_500_SCENARIO.request.service_name:
        raise ValueError("动态冻结 Worker 仅支持 order-service 发布故障演练")
    latest = max(item.observed_at for item in RELEASE_500_SCENARIO.evidence)
    offset = request.started_at - latest
    scenario = RELEASE_500_SCENARIO.model_copy(
        update={
            "scenario_id": "release-500-worker-v1",
            "request": request,
            "evidence": tuple(
                item.model_copy(update={"observed_at": item.observed_at + offset})
                for item in RELEASE_500_SCENARIO.evidence
            ),
        }
    )
    return FrozenScenarioDataSource(scenario)


class CurrentDynamicGraphFactory:
    """来源和图版本绑定运行，重试不依赖当前日期生成新材料。"""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        model: ChatOpenAI,
        checkpointer: BaseCheckpointSaver[str],
        clock: Callable[[], datetime],
        data_source: LiveInvestigationDataSource | None = None,
        query_executor: ReadOnlyQueryExecutor | None = None,
    ) -> None:
        self._sessions = session_factory
        self._model = model
        self._checkpointer = checkpointer
        self._clock = clock
        self._source = data_source
        self._query_executor = query_executor

    def __call__(self, context: ProcessorContext) -> DynamicThirdRoundRoutingGraph:
        source: FrozenScenarioDataSource | LiveInvestigationDataSource
        if context.data_source_mode is InvestigationDataSourceMode.LIVE:
            if self._source is None or self._query_executor is None:
                raise ValueError("现场动态图未注入MCP来源和受限取证执行器")
            source = self._source
        else:
            source = worker_frozen_source(context.request)
        return DynamicThirdRoundRoutingGraphFactory(
            session_factory=self._sessions,
            data_source=source,
            model=self._model,
            checkpointer=self._checkpointer,
            clock=self._clock,
            query_executor=self._query_executor,
        )(context)
