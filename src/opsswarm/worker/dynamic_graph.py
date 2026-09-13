"""根据持久运行契约装配 v3 首轮协调图。"""

from collections.abc import Callable
from datetime import datetime

from langchain_openai import ChatOpenAI
from langgraph.checkpoint.base import BaseCheckpointSaver
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.investigation.budget.policy import CallBudgetExceeded
from opsswarm.investigation.coordination.planner import CoordinationPlanner
from opsswarm.investigation.dynamic_graph import DynamicCoordinationGraph
from opsswarm.investigation.graph_thread import build_graph_thread_id
from opsswarm.investigation.models import InvestigationDataSourceMode, InvestigationMode
from opsswarm.investigation.scenarios import FrozenScenarioDataSource
from opsswarm.investigation.tasks.store import utc_now
from opsswarm.investigation.workflow_versions import (
    DYNAMIC_INVESTIGATION_GRAPH_VERSION,
    WorkflowVersion,
)
from opsswarm.worker.runner import ProcessorContext


class DynamicCoordinationGraphFactory:
    """只接受完整 v3 启动快照，并注入当前父租约与冻结证据边界。"""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        data_source: FrozenScenarioDataSource,
        model: ChatOpenAI,
        checkpointer: BaseCheckpointSaver[str],
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if not isinstance(data_source, FrozenScenarioDataSource):
            raise ValueError("v3 动态协调图需要冻结场景数据源")
        self._session_factory = session_factory
        self._source = data_source
        self._model = model
        self._checkpointer = checkpointer
        self._clock = clock

    def __call__(self, context: ProcessorContext) -> DynamicCoordinationGraph:
        if (
            context.workflow_version is not WorkflowVersion.V3
            or context.graph_version != DYNAMIC_INVESTIGATION_GRAPH_VERSION
        ):
            raise ValueError("动态协调图只接受指定图版本的 v3 调查")
        if (
            context.mode is not InvestigationMode.MULTI_AGENT
            or context.data_source_mode is not InvestigationDataSourceMode.FROZEN
            or context.investigation_run_id is None
            or context.lease_token is None
            or context.graph_thread_id is None
            or context.graph_thread_id != build_graph_thread_id(context.investigation_run_id)
            or context.coordination_policy is None
            or context.call_budget is None
            or context.model_budget is None
            or context.context_policy_version != "summary-context-v1"
            or context.evidence_policy_version != "task-evidence-v1"
            or context.observation_cutoff_at is not None
        ):
            raise ValueError("v3 动态协调图缺少完整的冻结运行契约")
        profile = context.model_budget.profile
        if (
            profile.structured_output_method != "function_calling"
            or self._model.model_name != profile.model_name
            or self._model.max_retries != 0
            or self._model.n != 1
        ):
            raise CallBudgetExceeded("model_budget_profile_mismatch")
        return DynamicCoordinationGraph(
            run_id=context.investigation_run_id,
            planner=CoordinationPlanner(
                self._session_factory,
                run_id=context.investigation_run_id,
                lease_token=context.lease_token,
                model=self._model,
                clock=self._clock,
            ),
            policy=context.coordination_policy,
            observation_cutoff_at=self._source.snapshot_time,
            evidence_snapshot_id=self._source.snapshot_id,
            checkpointer=self._checkpointer,
            graph_version=context.graph_version,
        )
