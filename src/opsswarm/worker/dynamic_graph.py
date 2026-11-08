"""保留历史 v3 图装配，并兼容导出当前图工厂。"""

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
from opsswarm.investigation.dynamic_follow_up_graph import DynamicFollowUpGraph
from opsswarm.investigation.dynamic_graph import DynamicCoordinationGraph
from opsswarm.investigation.dynamic_review_graph import DynamicReviewGraph
from opsswarm.investigation.dynamic_routing_graph import DynamicRoutingGraph
from opsswarm.investigation.dynamic_second_round_graph import DynamicSecondRoundGraph
from opsswarm.investigation.dynamic_second_round_review_graph import DynamicSecondRoundReviewGraph
from opsswarm.investigation.dynamic_second_round_routing_graph import (
    DynamicSecondRoundRoutingGraph,
)
from opsswarm.investigation.dynamic_second_round_synthesis_graph import (
    DynamicSecondRoundSynthesisGraph,
)
from opsswarm.investigation.dynamic_synthesis_graph import DynamicSynthesisGraph
from opsswarm.investigation.dynamic_third_round_graph import DynamicThirdRoundGraph
from opsswarm.investigation.dynamic_third_round_planning_graph import (
    DynamicThirdRoundPlanningGraph,
)
from opsswarm.investigation.dynamic_third_round_review_graph import DynamicThirdRoundReviewGraph
from opsswarm.investigation.dynamic_third_round_synthesis_graph import (
    DynamicThirdRoundSynthesisGraph,
)
from opsswarm.investigation.follow_up_planning.service import (
    FollowUpPlanService,
    ThirdRoundPlanService,
)
from opsswarm.investigation.graph_thread import build_graph_thread_id
from opsswarm.investigation.loop.agent import EvidenceLoopAnalyst
from opsswarm.investigation.loop.service import EvidenceLoopService
from opsswarm.investigation.models import InvestigationDataSourceMode, InvestigationMode
from opsswarm.investigation.review.service import (
    IndependentHypothesisReviewer,
    SecondRoundHypothesisReviewer,
    ThirdRoundHypothesisReviewer,
)
from opsswarm.investigation.review_routing.service import (
    ReviewRoutingService,
    SecondRoundReviewRoutingService,
)
from opsswarm.investigation.scenarios import FrozenScenarioDataSource
from opsswarm.investigation.synthesis.service import (
    FirstRoundSynthesizer,
    SecondRoundSynthesizer,
    ThirdRoundSynthesizer,
)
from opsswarm.investigation.tasks.evidence_store import TaskEvidenceStore
from opsswarm.investigation.tasks.queries import CollaborationQueries
from opsswarm.investigation.tasks.runtime import DynamicTaskRuntime
from opsswarm.investigation.tasks.store import InvestigationTaskStore, utc_now
from opsswarm.investigation.workflow_versions import (
    LEGACY_DYNAMIC_COORDINATION_GRAPH_VERSION,
    LEGACY_DYNAMIC_FIRST_ROUND_GRAPH_VERSION,
    LEGACY_DYNAMIC_FOLLOW_UP_GRAPH_VERSION,
    LEGACY_DYNAMIC_REVIEW_GRAPH_VERSION,
    LEGACY_DYNAMIC_ROUTING_GRAPH_VERSION,
    LEGACY_DYNAMIC_SECOND_ROUND_GRAPH_VERSION,
    LEGACY_DYNAMIC_SECOND_ROUND_REVIEW_GRAPH_VERSION,
    LEGACY_DYNAMIC_SECOND_ROUND_ROUTING_GRAPH_VERSION,
    LEGACY_DYNAMIC_SECOND_ROUND_SYNTHESIS_GRAPH_VERSION,
    LEGACY_DYNAMIC_SYNTHESIS_GRAPH_VERSION,
    LEGACY_DYNAMIC_THIRD_ROUND_GRAPH_VERSION,
    LEGACY_DYNAMIC_THIRD_ROUND_PLANNING_GRAPH_VERSION,
    LEGACY_DYNAMIC_THIRD_ROUND_REVIEW_GRAPH_VERSION,
    LEGACY_DYNAMIC_THIRD_ROUND_SYNTHESIS_GRAPH_VERSION,
    WorkflowVersion,
)
from opsswarm.worker.dynamic_factory import (
    DynamicThirdRoundRoutingGraphFactory as DynamicThirdRoundRoutingGraphFactory,
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
            or context.graph_version != LEGACY_DYNAMIC_REVIEW_GRAPH_VERSION
        ):
            raise ValueError("动态审查图只接受指定图版本的 v3 调查")
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


class DynamicRoutingGraphFactory:
    """按图 v5 装配动态首轮、独立审查与确定性路由。"""

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
            raise ValueError("v3 动态路由图需要冻结场景数据源")
        self._session_factory = session_factory
        self._source = data_source
        self._model = model
        self._checkpointer = checkpointer
        self._clock = clock

    def __call__(self, context: ProcessorContext) -> DynamicRoutingGraph:
        if (
            context.workflow_version is not WorkflowVersion.V3
            or context.graph_version != LEGACY_DYNAMIC_ROUTING_GRAPH_VERSION
        ):
            raise ValueError("动态路由图只接受指定图版本的 v3 调查")
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
            raise ValueError("v3 动态路由图缺少完整的冻结运行契约")
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
        router = ReviewRoutingService(
            self._session_factory,
            run_id=context.investigation_run_id,
            lease_token=context.lease_token,
            clock=self._clock,
        )
        return DynamicRoutingGraph(
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
            router=router,
            checkpointer=self._checkpointer,
        )


class DynamicFollowUpGraphFactory:
    """按图 v6 装配首轮审查路由与第二轮确定性计划。"""

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
            raise ValueError("v3 补查计划图需要冻结场景数据源")
        self._session_factory = session_factory
        self._source = data_source
        self._model = model
        self._checkpointer = checkpointer
        self._clock = clock

    def __call__(self, context: ProcessorContext) -> DynamicFollowUpGraph:
        if (
            context.workflow_version is not WorkflowVersion.V3
            or context.graph_version != LEGACY_DYNAMIC_FOLLOW_UP_GRAPH_VERSION
        ):
            raise ValueError("补查计划图只接受指定图版本的 v3 调查")
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
            raise ValueError("v3 补查计划图缺少完整的冻结运行契约")
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
        router = ReviewRoutingService(
            self._session_factory,
            run_id=context.investigation_run_id,
            lease_token=context.lease_token,
            clock=self._clock,
        )
        return DynamicFollowUpGraph(
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
            router=router,
            follow_up_planner=FollowUpPlanService(
                self._session_factory,
                run_id=context.investigation_run_id,
                lease_token=context.lease_token,
                clock=self._clock,
            ),
            checkpointer=self._checkpointer,
        )


class DynamicSecondRoundGraphFactory:
    """按图 v7 装配第二轮计划与专业任务执行。"""

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
            raise ValueError("v3 第二轮执行图需要冻结场景数据源")
        self._session_factory = session_factory
        self._source = data_source
        self._model = model
        self._checkpointer = checkpointer
        self._clock = clock

    def __call__(self, context: ProcessorContext) -> DynamicSecondRoundGraph:
        if (
            context.workflow_version is not WorkflowVersion.V3
            or context.graph_version != LEGACY_DYNAMIC_SECOND_ROUND_GRAPH_VERSION
        ):
            raise ValueError("第二轮执行图只接受图 v7 的 v3 调查")
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
            raise ValueError("v3 第二轮执行图缺少完整的冻结运行契约")
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
        router = ReviewRoutingService(
            self._session_factory,
            run_id=context.investigation_run_id,
            lease_token=context.lease_token,
            clock=self._clock,
        )
        return DynamicSecondRoundGraph(
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
            router=router,
            follow_up_planner=FollowUpPlanService(
                self._session_factory,
                run_id=context.investigation_run_id,
                lease_token=context.lease_token,
                clock=self._clock,
            ),
            checkpointer=self._checkpointer,
        )


class DynamicSecondRoundSynthesisGraphFactory:
    """按图 v8 装配第二轮任务执行与新开放假设综合。"""

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
            raise ValueError("v3 第二轮综合图需要冻结场景数据源")
        self._session_factory = session_factory
        self._source = data_source
        self._model = model
        self._checkpointer = checkpointer
        self._clock = clock

    def __call__(self, context: ProcessorContext) -> DynamicSecondRoundSynthesisGraph:
        if (
            context.workflow_version is not WorkflowVersion.V3
            or context.graph_version != LEGACY_DYNAMIC_SECOND_ROUND_SYNTHESIS_GRAPH_VERSION
        ):
            raise ValueError("第二轮综合图只接受图 v8 的 v3 调查")
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
            raise ValueError("v3 第二轮综合图缺少完整的冻结运行契约")
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
        router = ReviewRoutingService(
            self._session_factory,
            run_id=context.investigation_run_id,
            lease_token=context.lease_token,
            clock=self._clock,
        )
        return DynamicSecondRoundSynthesisGraph(
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
            router=router,
            follow_up_planner=FollowUpPlanService(
                self._session_factory,
                run_id=context.investigation_run_id,
                lease_token=context.lease_token,
                clock=self._clock,
            ),
            follow_up_synthesizer=SecondRoundSynthesizer(
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


class DynamicSecondRoundReviewGraphFactory:
    """按图 v9 装配第二轮综合后的独立审查。"""

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
            raise ValueError("v3 第二轮独立审查图需要冻结场景数据源")
        self._session_factory = session_factory
        self._source = data_source
        self._model = model
        self._checkpointer = checkpointer
        self._clock = clock

    def __call__(self, context: ProcessorContext) -> DynamicSecondRoundReviewGraph:
        if (
            context.workflow_version is not WorkflowVersion.V3
            or context.graph_version != LEGACY_DYNAMIC_SECOND_ROUND_REVIEW_GRAPH_VERSION
        ):
            raise ValueError("第二轮独立审查图只接受图 v9 的 v3 调查")
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
            raise ValueError("v3 第二轮独立审查图缺少完整的冻结运行契约")
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
        router = ReviewRoutingService(
            self._session_factory,
            run_id=context.investigation_run_id,
            lease_token=context.lease_token,
            clock=self._clock,
        )
        return DynamicSecondRoundReviewGraph(
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
            router=router,
            follow_up_planner=FollowUpPlanService(
                self._session_factory,
                run_id=context.investigation_run_id,
                lease_token=context.lease_token,
                clock=self._clock,
            ),
            follow_up_synthesizer=SecondRoundSynthesizer(
                self._session_factory,
                run_id=context.investigation_run_id,
                lease_token=context.lease_token,
                model=self._model,
                policy=context.model_budget,
                runner=runner,
                clock=self._clock,
            ),
            follow_up_reviewer=SecondRoundHypothesisReviewer(
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


class DynamicSecondRoundRoutingGraphFactory:
    """按图 v10 装配第二轮独立审查后的确定性路由。"""

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
            raise ValueError("v3 第二轮确定性路由图需要冻结场景数据源")
        self._session_factory = session_factory
        self._source = data_source
        self._model = model
        self._checkpointer = checkpointer
        self._clock = clock

    def __call__(self, context: ProcessorContext) -> DynamicSecondRoundRoutingGraph:
        if (
            context.workflow_version is not WorkflowVersion.V3
            or context.graph_version != LEGACY_DYNAMIC_SECOND_ROUND_ROUTING_GRAPH_VERSION
        ):
            raise ValueError("第二轮确定性路由图只接受图 v10 的 v3 调查")
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
            raise ValueError("v3 第二轮确定性路由图缺少完整的冻结运行契约")
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
        router = ReviewRoutingService(
            self._session_factory,
            run_id=context.investigation_run_id,
            lease_token=context.lease_token,
            clock=self._clock,
        )
        return DynamicSecondRoundRoutingGraph(
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
            router=router,
            follow_up_planner=FollowUpPlanService(
                self._session_factory,
                run_id=context.investigation_run_id,
                lease_token=context.lease_token,
                clock=self._clock,
            ),
            follow_up_synthesizer=SecondRoundSynthesizer(
                self._session_factory,
                run_id=context.investigation_run_id,
                lease_token=context.lease_token,
                model=self._model,
                policy=context.model_budget,
                runner=runner,
                clock=self._clock,
            ),
            follow_up_reviewer=SecondRoundHypothesisReviewer(
                self._session_factory,
                run_id=context.investigation_run_id,
                lease_token=context.lease_token,
                model=self._model,
                policy=context.model_budget,
                runner=runner,
                clock=self._clock,
            ),
            follow_up_router=SecondRoundReviewRoutingService(
                self._session_factory,
                run_id=context.investigation_run_id,
                lease_token=context.lease_token,
                clock=self._clock,
            ),
            checkpointer=self._checkpointer,
        )


class DynamicThirdRoundPlanningGraphFactory:
    """按图 v11 装配第二轮补查路由后的第三轮计划。"""

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
            raise ValueError("v3 第三轮计划图需要冻结场景数据源")
        self._session_factory = session_factory
        self._source = data_source
        self._model = model
        self._checkpointer = checkpointer
        self._clock = clock

    def __call__(self, context: ProcessorContext) -> DynamicThirdRoundPlanningGraph:
        if (
            context.workflow_version is not WorkflowVersion.V3
            or context.graph_version != LEGACY_DYNAMIC_THIRD_ROUND_PLANNING_GRAPH_VERSION
        ):
            raise ValueError("第三轮计划图只接受图 v11 的 v3 调查")
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
            raise ValueError("v3 第三轮计划图缺少完整的冻结运行契约")
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
        router = ReviewRoutingService(
            self._session_factory,
            run_id=context.investigation_run_id,
            lease_token=context.lease_token,
            clock=self._clock,
        )
        return DynamicThirdRoundPlanningGraph(
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
            router=router,
            follow_up_planner=FollowUpPlanService(
                self._session_factory,
                run_id=context.investigation_run_id,
                lease_token=context.lease_token,
                clock=self._clock,
            ),
            follow_up_synthesizer=SecondRoundSynthesizer(
                self._session_factory,
                run_id=context.investigation_run_id,
                lease_token=context.lease_token,
                model=self._model,
                policy=context.model_budget,
                runner=runner,
                clock=self._clock,
            ),
            follow_up_reviewer=SecondRoundHypothesisReviewer(
                self._session_factory,
                run_id=context.investigation_run_id,
                lease_token=context.lease_token,
                model=self._model,
                policy=context.model_budget,
                runner=runner,
                clock=self._clock,
            ),
            follow_up_router=SecondRoundReviewRoutingService(
                self._session_factory,
                run_id=context.investigation_run_id,
                lease_token=context.lease_token,
                clock=self._clock,
            ),
            third_round_planner=ThirdRoundPlanService(
                self._session_factory,
                run_id=context.investigation_run_id,
                lease_token=context.lease_token,
                clock=self._clock,
            ),
            checkpointer=self._checkpointer,
        )


class DynamicThirdRoundGraphFactory:
    """按图 v12 装配第三轮专业任务执行。"""

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
            raise ValueError("v3 第三轮专业调查图需要冻结场景数据源")
        self._session_factory = session_factory
        self._source = data_source
        self._model = model
        self._checkpointer = checkpointer
        self._clock = clock

    def __call__(self, context: ProcessorContext) -> DynamicThirdRoundGraph:
        if (
            context.workflow_version is not WorkflowVersion.V3
            or context.graph_version != LEGACY_DYNAMIC_THIRD_ROUND_GRAPH_VERSION
        ):
            raise ValueError("第三轮专业调查图只接受图 v12 的 v3 调查")
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
            raise ValueError("v3 第三轮专业调查图缺少完整的冻结运行契约")
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
        router = ReviewRoutingService(
            self._session_factory,
            run_id=context.investigation_run_id,
            lease_token=context.lease_token,
            clock=self._clock,
        )
        return DynamicThirdRoundGraph(
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
            router=router,
            follow_up_planner=FollowUpPlanService(
                self._session_factory,
                run_id=context.investigation_run_id,
                lease_token=context.lease_token,
                clock=self._clock,
            ),
            follow_up_synthesizer=SecondRoundSynthesizer(
                self._session_factory,
                run_id=context.investigation_run_id,
                lease_token=context.lease_token,
                model=self._model,
                policy=context.model_budget,
                runner=runner,
                clock=self._clock,
            ),
            follow_up_reviewer=SecondRoundHypothesisReviewer(
                self._session_factory,
                run_id=context.investigation_run_id,
                lease_token=context.lease_token,
                model=self._model,
                policy=context.model_budget,
                runner=runner,
                clock=self._clock,
            ),
            follow_up_router=SecondRoundReviewRoutingService(
                self._session_factory,
                run_id=context.investigation_run_id,
                lease_token=context.lease_token,
                clock=self._clock,
            ),
            third_round_planner=ThirdRoundPlanService(
                self._session_factory,
                run_id=context.investigation_run_id,
                lease_token=context.lease_token,
                clock=self._clock,
            ),
            checkpointer=self._checkpointer,
        )


class DynamicThirdRoundSynthesisGraphFactory:
    """按图 v13 装配第三轮综合与开放假设恢复。"""

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
            raise ValueError("v3 第三轮综合图需要冻结场景数据源")
        self._session_factory = session_factory
        self._source = data_source
        self._model = model
        self._checkpointer = checkpointer
        self._clock = clock

    def __call__(self, context: ProcessorContext) -> DynamicThirdRoundSynthesisGraph:
        if (
            context.workflow_version is not WorkflowVersion.V3
            or context.graph_version != LEGACY_DYNAMIC_THIRD_ROUND_SYNTHESIS_GRAPH_VERSION
        ):
            raise ValueError("第三轮综合图只接受图 v13 的 v3 调查")
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
            raise ValueError("v3 第三轮综合图缺少完整的冻结运行契约")
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
        router = ReviewRoutingService(
            self._session_factory,
            run_id=context.investigation_run_id,
            lease_token=context.lease_token,
            clock=self._clock,
        )
        return DynamicThirdRoundSynthesisGraph(
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
            router=router,
            follow_up_planner=FollowUpPlanService(
                self._session_factory,
                run_id=context.investigation_run_id,
                lease_token=context.lease_token,
                clock=self._clock,
            ),
            follow_up_synthesizer=SecondRoundSynthesizer(
                self._session_factory,
                run_id=context.investigation_run_id,
                lease_token=context.lease_token,
                model=self._model,
                policy=context.model_budget,
                runner=runner,
                clock=self._clock,
            ),
            follow_up_reviewer=SecondRoundHypothesisReviewer(
                self._session_factory,
                run_id=context.investigation_run_id,
                lease_token=context.lease_token,
                model=self._model,
                policy=context.model_budget,
                runner=runner,
                clock=self._clock,
            ),
            follow_up_router=SecondRoundReviewRoutingService(
                self._session_factory,
                run_id=context.investigation_run_id,
                lease_token=context.lease_token,
                clock=self._clock,
            ),
            third_round_planner=ThirdRoundPlanService(
                self._session_factory,
                run_id=context.investigation_run_id,
                lease_token=context.lease_token,
                clock=self._clock,
            ),
            third_round_synthesizer=ThirdRoundSynthesizer(
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


class DynamicThirdRoundReviewGraphFactory:
    """按图 v14 装配第三轮独立审查与恢复。"""

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
            raise ValueError("v3 第三轮审查图需要冻结场景数据源")
        self._session_factory = session_factory
        self._source = data_source
        self._model = model
        self._checkpointer = checkpointer
        self._clock = clock

    def __call__(self, context: ProcessorContext) -> DynamicThirdRoundReviewGraph:
        if (
            context.workflow_version is not WorkflowVersion.V3
            or context.graph_version != LEGACY_DYNAMIC_THIRD_ROUND_REVIEW_GRAPH_VERSION
        ):
            raise ValueError("第三轮审查图只接受图 v14 的 v3 调查")
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
            raise ValueError("v3 第三轮审查图缺少完整的冻结运行契约")
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
        router = ReviewRoutingService(
            self._session_factory,
            run_id=context.investigation_run_id,
            lease_token=context.lease_token,
            clock=self._clock,
        )
        return DynamicThirdRoundReviewGraph(
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
            router=router,
            follow_up_planner=FollowUpPlanService(
                self._session_factory,
                run_id=context.investigation_run_id,
                lease_token=context.lease_token,
                clock=self._clock,
            ),
            follow_up_synthesizer=SecondRoundSynthesizer(
                self._session_factory,
                run_id=context.investigation_run_id,
                lease_token=context.lease_token,
                model=self._model,
                policy=context.model_budget,
                runner=runner,
                clock=self._clock,
            ),
            follow_up_reviewer=SecondRoundHypothesisReviewer(
                self._session_factory,
                run_id=context.investigation_run_id,
                lease_token=context.lease_token,
                model=self._model,
                policy=context.model_budget,
                runner=runner,
                clock=self._clock,
            ),
            follow_up_router=SecondRoundReviewRoutingService(
                self._session_factory,
                run_id=context.investigation_run_id,
                lease_token=context.lease_token,
                clock=self._clock,
            ),
            third_round_planner=ThirdRoundPlanService(
                self._session_factory,
                run_id=context.investigation_run_id,
                lease_token=context.lease_token,
                clock=self._clock,
            ),
            third_round_synthesizer=ThirdRoundSynthesizer(
                self._session_factory,
                run_id=context.investigation_run_id,
                lease_token=context.lease_token,
                model=self._model,
                policy=context.model_budget,
                runner=runner,
                clock=self._clock,
            ),
            third_round_reviewer=ThirdRoundHypothesisReviewer(
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
