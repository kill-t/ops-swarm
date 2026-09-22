"""根据持久运行契约装配 v3 工作流的四个已发布图版本。"""

from collections.abc import Callable
from datetime import datetime

from langchain_openai import ChatOpenAI
from langgraph.checkpoint.base import BaseCheckpointSaver
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.investigation.budget.policy import CallBudgetExceeded
from opsswarm.investigation.budget.runtime import BudgetedCallRunner
from opsswarm.investigation.budget.store import CallBudgetStore
from opsswarm.investigation.coordination.planner import CoordinationPlanner
from opsswarm.investigation.dynamic_first_round_graph import DynamicFirstRoundGraph
from opsswarm.investigation.dynamic_graph import DynamicCoordinationGraph
from opsswarm.investigation.dynamic_review_graph import DynamicReviewGraph
from opsswarm.investigation.dynamic_synthesis_graph import DynamicSynthesisGraph
from opsswarm.investigation.graph_thread import build_graph_thread_id
from opsswarm.investigation.loop.agent import EvidenceLoopAnalyst
from opsswarm.investigation.loop.service import EvidenceLoopService
from opsswarm.investigation.models import InvestigationDataSourceMode, InvestigationMode
from opsswarm.investigation.review.service import IndependentHypothesisReviewer
from opsswarm.investigation.scenarios import FrozenScenarioDataSource
from opsswarm.investigation.synthesis.service import FirstRoundSynthesizer
from opsswarm.investigation.tasks.evidence_store import TaskEvidenceStore
from opsswarm.investigation.tasks.queries import CollaborationQueries
from opsswarm.investigation.tasks.runtime import DynamicTaskRuntime
from opsswarm.investigation.tasks.store import InvestigationTaskStore, utc_now
from opsswarm.investigation.workflow_versions import (
    DYNAMIC_INVESTIGATION_GRAPH_VERSION,
    LEGACY_DYNAMIC_COORDINATION_GRAPH_VERSION,
    LEGACY_DYNAMIC_FIRST_ROUND_GRAPH_VERSION,
    LEGACY_DYNAMIC_SYNTHESIS_GRAPH_VERSION,
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
            or context.graph_version != LEGACY_DYNAMIC_COORDINATION_GRAPH_VERSION
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


class DynamicFirstRoundGraphFactory:
    """按当前 v3 图版本装配协调、动态专业循环和持久任务运行器。"""

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
            raise ValueError("v3 动态专业图需要冻结场景数据源")
        self._session_factory = session_factory
        self._source = data_source
        self._model = model
        self._checkpointer = checkpointer
        self._clock = clock

    def __call__(self, context: ProcessorContext) -> DynamicFirstRoundGraph:
        if (
            context.workflow_version is not WorkflowVersion.V3
            or context.graph_version != LEGACY_DYNAMIC_FIRST_ROUND_GRAPH_VERSION
        ):
            raise ValueError("动态专业图只接受当前图版本的 v3 调查")
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
            raise ValueError("v3 动态专业图缺少完整的冻结运行契约")
        profile = context.model_budget.profile
        if (
            profile.structured_output_method != "function_calling"
            or self._model.model_name != profile.model_name
            or self._model.max_retries != 0
            or self._model.n != 1
        ):
            raise CallBudgetExceeded("model_budget_profile_mismatch")
        runner = BudgetedCallRunner(
            run_id=context.investigation_run_id,
            lease_token=context.lease_token,
            store=CallBudgetStore(self._session_factory, clock=self._clock),
        )
        runtime = DynamicTaskRuntime(
            run_id=context.investigation_run_id,
            lease_token=context.lease_token,
            store=InvestigationTaskStore(self._session_factory, clock=self._clock),
            queries=CollaborationQueries(self._session_factory),
            snapshot_id=self._source.snapshot_id,
            snapshot_time=self._source.snapshot_time,
            data_source_mode="frozen",
            call_runner=runner,
            model_input_budget=True,
            evidence_store=TaskEvidenceStore(self._session_factory, clock=self._clock),
        )
        return DynamicFirstRoundGraph(
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
            data_source=self._source,
            analyst=EvidenceLoopAnalyst(
                self._model,
                policy=context.model_budget,
                runner=runner,
                service=EvidenceLoopService(self._session_factory, context.investigation_run_id),
            ),
            task_runtime=runtime,
            checkpointer=self._checkpointer,
        )


class DynamicSynthesisGraphFactory:
    """按图 v3 装配动态首轮、协调综合与开放假设持久化。"""

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
            raise ValueError("v3 动态综合图需要冻结场景数据源")
        self._session_factory = session_factory
        self._source = data_source
        self._model = model
        self._checkpointer = checkpointer
        self._clock = clock

    def __call__(self, context: ProcessorContext) -> DynamicSynthesisGraph:
        if (
            context.workflow_version is not WorkflowVersion.V3
            or context.graph_version != LEGACY_DYNAMIC_SYNTHESIS_GRAPH_VERSION
        ):
            raise ValueError("动态综合图只接受当前图版本的 v3 调查")
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
            raise ValueError("v3 动态综合图缺少完整的冻结运行契约")
        profile = context.model_budget.profile
        if (
            profile.structured_output_method != "function_calling"
            or self._model.model_name != profile.model_name
            or self._model.max_retries != 0
            or self._model.n != 1
        ):
            raise CallBudgetExceeded("model_budget_profile_mismatch")
        runner = BudgetedCallRunner(
            run_id=context.investigation_run_id,
            lease_token=context.lease_token,
            store=CallBudgetStore(self._session_factory, clock=self._clock),
        )
        runtime = DynamicTaskRuntime(
            run_id=context.investigation_run_id,
            lease_token=context.lease_token,
            store=InvestigationTaskStore(self._session_factory, clock=self._clock),
            queries=CollaborationQueries(self._session_factory),
            snapshot_id=self._source.snapshot_id,
            snapshot_time=self._source.snapshot_time,
            data_source_mode="frozen",
            call_runner=runner,
            model_input_budget=True,
            evidence_store=TaskEvidenceStore(self._session_factory, clock=self._clock),
        )
        return DynamicSynthesisGraph(
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
            data_source=self._source,
            analyst=EvidenceLoopAnalyst(
                self._model,
                policy=context.model_budget,
                runner=runner,
                service=EvidenceLoopService(self._session_factory, context.investigation_run_id),
            ),
            task_runtime=runtime,
            synthesizer=FirstRoundSynthesizer(
                self._session_factory,
                run_id=context.investigation_run_id,
                lease_token=context.lease_token,
                model=self._model,
                policy=context.model_budget,
                runner=runner,
                clock=self._clock,
            ),
            checkpointer=self._checkpointer,
        )


class DynamicReviewGraphFactory:
    """按图 v4 装配动态首轮、开放假设与独立审查。"""

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
            raise ValueError("v3 动态审查图需要冻结场景数据源")
        self._session_factory = session_factory
        self._source = data_source
        self._model = model
        self._checkpointer = checkpointer
        self._clock = clock

    def __call__(self, context: ProcessorContext) -> DynamicReviewGraph:
        if (
            context.workflow_version is not WorkflowVersion.V3
            or context.graph_version != DYNAMIC_INVESTIGATION_GRAPH_VERSION
        ):
            raise ValueError("动态审查图只接受当前图版本的 v3 调查")
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
            raise ValueError("v3 动态审查图缺少完整的冻结运行契约")
        profile = context.model_budget.profile
        if (
            profile.structured_output_method != "function_calling"
            or self._model.model_name != profile.model_name
            or self._model.max_retries != 0
            or self._model.n != 1
        ):
            raise CallBudgetExceeded("model_budget_profile_mismatch")
        runner = BudgetedCallRunner(
            run_id=context.investigation_run_id,
            lease_token=context.lease_token,
            store=CallBudgetStore(self._session_factory, clock=self._clock),
        )
        runtime = DynamicTaskRuntime(
            run_id=context.investigation_run_id,
            lease_token=context.lease_token,
            store=InvestigationTaskStore(self._session_factory, clock=self._clock),
            queries=CollaborationQueries(self._session_factory),
            snapshot_id=self._source.snapshot_id,
            snapshot_time=self._source.snapshot_time,
            data_source_mode="frozen",
            call_runner=runner,
            model_input_budget=True,
            evidence_store=TaskEvidenceStore(self._session_factory, clock=self._clock),
        )
        synthesizer = FirstRoundSynthesizer(
            self._session_factory,
            run_id=context.investigation_run_id,
            lease_token=context.lease_token,
            model=self._model,
            policy=context.model_budget,
            runner=runner,
            clock=self._clock,
        )
        reviewer = IndependentHypothesisReviewer(
            self._session_factory,
            run_id=context.investigation_run_id,
            lease_token=context.lease_token,
            model=self._model,
            policy=context.model_budget,
            runner=runner,
            clock=self._clock,
        )
        return DynamicReviewGraph(
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
            data_source=self._source,
            analyst=EvidenceLoopAnalyst(
                self._model,
                policy=context.model_budget,
                runner=runner,
                service=EvidenceLoopService(self._session_factory, context.investigation_run_id),
            ),
            task_runtime=runtime,
            synthesizer=synthesizer,
            reviewer=reviewer,
            checkpointer=self._checkpointer,
        )
