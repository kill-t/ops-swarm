"""保留历史 v3 图装配，并兼容导出当前图工厂。"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from functools import cached_property
from typing import ClassVar
from uuid import UUID

from langchain_openai import ChatOpenAI
from langgraph.checkpoint.base import BaseCheckpointSaver
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.investigation.budget.model_contracts import ModelBudgetPolicy
from opsswarm.investigation.budget.policy import CallBudgetExceeded
from opsswarm.investigation.budget.runtime import BudgetedCallRunner
from opsswarm.investigation.budget.store import CallBudgetStore
from opsswarm.investigation.coordination.contracts import CoordinationPolicy
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
    InvestigationGraphVersion,
    WorkflowVersion,
)
from opsswarm.worker.dynamic_factory import (
    DynamicThirdRoundRoutingGraphFactory as DynamicThirdRoundRoutingGraphFactory,
)
from opsswarm.worker.runner import ProcessorContext


@dataclass
class _FrozenGraphAssembly:
    """单次历史图装配的依赖；阶段按需构造并共享同一运行预算。"""

    sessions: async_sessionmaker[AsyncSession]
    source: FrozenScenarioDataSource
    model: ChatOpenAI
    checkpointer: BaseCheckpointSaver[str]
    clock: Callable[[], datetime]
    run_id: UUID
    lease_token: UUID
    graph_version: InvestigationGraphVersion
    policy: CoordinationPolicy
    model_policy: ModelBudgetPolicy

    @cached_property
    def planner(self) -> CoordinationPlanner:
        return CoordinationPlanner(
            self.sessions,
            run_id=self.run_id,
            lease_token=self.lease_token,
            model=self.model,
            clock=self.clock,
        )

    @cached_property
    def runner(self) -> BudgetedCallRunner:
        return BudgetedCallRunner(
            run_id=self.run_id,
            lease_token=self.lease_token,
            store=CallBudgetStore(self.sessions, clock=self.clock),
        )

    @cached_property
    def runtime(self) -> DynamicTaskRuntime:
        return DynamicTaskRuntime(
            run_id=self.run_id,
            lease_token=self.lease_token,
            store=InvestigationTaskStore(self.sessions, clock=self.clock),
            queries=CollaborationQueries(self.sessions),
            snapshot_id=self.source.snapshot_id,
            snapshot_time=self.source.snapshot_time,
            data_source_mode="frozen",
            call_runner=self.runner,
            model_input_budget=True,
            evidence_store=TaskEvidenceStore(self.sessions, clock=self.clock),
        )

    @cached_property
    def analyst(self) -> EvidenceLoopAnalyst:
        return EvidenceLoopAnalyst(
            self.model,
            policy=self.model_policy,
            runner=self.runner,
            service=EvidenceLoopService(self.sessions, self.run_id),
        )

    @cached_property
    def synthesizer(self) -> FirstRoundSynthesizer:
        return FirstRoundSynthesizer(
            self.sessions,
            run_id=self.run_id,
            lease_token=self.lease_token,
            model=self.model,
            policy=self.model_policy,
            runner=self.runner,
            clock=self.clock,
        )

    @cached_property
    def reviewer(self) -> IndependentHypothesisReviewer:
        return IndependentHypothesisReviewer(
            self.sessions,
            run_id=self.run_id,
            lease_token=self.lease_token,
            model=self.model,
            policy=self.model_policy,
            runner=self.runner,
            clock=self.clock,
        )

    @cached_property
    def router(self) -> ReviewRoutingService:
        return ReviewRoutingService(
            self.sessions, run_id=self.run_id, lease_token=self.lease_token, clock=self.clock
        )

    @cached_property
    def follow_up_planner(self) -> FollowUpPlanService:
        return FollowUpPlanService(
            self.sessions, run_id=self.run_id, lease_token=self.lease_token, clock=self.clock
        )

    @cached_property
    def follow_up_synthesizer(self) -> SecondRoundSynthesizer:
        return SecondRoundSynthesizer(
            self.sessions,
            run_id=self.run_id,
            lease_token=self.lease_token,
            model=self.model,
            policy=self.model_policy,
            runner=self.runner,
            clock=self.clock,
        )

    @cached_property
    def follow_up_reviewer(self) -> SecondRoundHypothesisReviewer:
        return SecondRoundHypothesisReviewer(
            self.sessions,
            run_id=self.run_id,
            lease_token=self.lease_token,
            model=self.model,
            policy=self.model_policy,
            runner=self.runner,
            clock=self.clock,
        )

    @cached_property
    def follow_up_router(self) -> SecondRoundReviewRoutingService:
        return SecondRoundReviewRoutingService(
            self.sessions, run_id=self.run_id, lease_token=self.lease_token, clock=self.clock
        )

    @cached_property
    def third_round_planner(self) -> ThirdRoundPlanService:
        return ThirdRoundPlanService(
            self.sessions, run_id=self.run_id, lease_token=self.lease_token, clock=self.clock
        )

    @cached_property
    def third_round_synthesizer(self) -> ThirdRoundSynthesizer:
        return ThirdRoundSynthesizer(
            self.sessions,
            run_id=self.run_id,
            lease_token=self.lease_token,
            model=self.model,
            policy=self.model_policy,
            runner=self.runner,
            clock=self.clock,
        )

    @cached_property
    def third_round_reviewer(self) -> ThirdRoundHypothesisReviewer:
        return ThirdRoundHypothesisReviewer(
            self.sessions,
            run_id=self.run_id,
            lease_token=self.lease_token,
            model=self.model,
            policy=self.model_policy,
            runner=self.runner,
            clock=self.clock,
        )


class _FrozenGraphFactory:
    """只共用历史冻结图的依赖准入，不扩大任何图版本的执行资格。"""

    _graph_version: ClassVar[InvestigationGraphVersion]
    _source_error: ClassVar[str]
    _version_error: ClassVar[str]
    _context_error: ClassVar[str]

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
            raise ValueError(self._source_error)
        self._session_factory = session_factory
        self._source = data_source
        self._model = model
        self._checkpointer = checkpointer
        self._clock = clock

    def _assembly(self, context: ProcessorContext) -> _FrozenGraphAssembly:
        if (
            context.workflow_version is not WorkflowVersion.V3
            or context.graph_version != self._graph_version
        ):
            raise ValueError(self._version_error)
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
            raise ValueError(self._context_error)
        profile = context.model_budget.profile
        if (
            profile.structured_output_method != "function_calling"
            or self._model.model_name != profile.model_name
            or self._model.max_retries != 0
            or self._model.n != 1
        ):
            raise CallBudgetExceeded("model_budget_profile_mismatch")
        return _FrozenGraphAssembly(
            sessions=self._session_factory,
            source=self._source,
            model=self._model,
            checkpointer=self._checkpointer,
            clock=self._clock,
            run_id=context.investigation_run_id,
            lease_token=context.lease_token,
            graph_version=self._graph_version,
            policy=context.coordination_policy,
            model_policy=context.model_budget,
        )


class DynamicCoordinationGraphFactory(_FrozenGraphFactory):
    """只接受完整 v3 启动快照，并注入当前父租约与冻结证据边界。"""

    _graph_version = LEGACY_DYNAMIC_COORDINATION_GRAPH_VERSION
    _source_error = "v3 动态协调图需要冻结场景数据源"
    _version_error = "动态协调图只接受指定图版本的 v3 调查"
    _context_error = "v3 动态协调图缺少完整的冻结运行契约"

    def __call__(self, context: ProcessorContext) -> DynamicCoordinationGraph:
        assembly = self._assembly(context)
        return DynamicCoordinationGraph(
            run_id=assembly.run_id,
            planner=assembly.planner,
            policy=assembly.policy,
            observation_cutoff_at=assembly.source.snapshot_time,
            evidence_snapshot_id=assembly.source.snapshot_id,
            checkpointer=assembly.checkpointer,
            graph_version=assembly.graph_version,
        )


class DynamicFirstRoundGraphFactory(_FrozenGraphFactory):
    """按当前 v3 图版本装配协调、动态专业循环和持久任务运行器。"""

    _graph_version = LEGACY_DYNAMIC_FIRST_ROUND_GRAPH_VERSION
    _source_error = "v3 动态专业图需要冻结场景数据源"
    _version_error = "动态专业图只接受当前图版本的 v3 调查"
    _context_error = "v3 动态专业图缺少完整的冻结运行契约"

    def __call__(self, context: ProcessorContext) -> DynamicFirstRoundGraph:
        assembly = self._assembly(context)
        return DynamicFirstRoundGraph(
            run_id=assembly.run_id,
            planner=assembly.planner,
            policy=assembly.policy,
            observation_cutoff_at=assembly.source.snapshot_time,
            evidence_snapshot_id=assembly.source.snapshot_id,
            data_source=assembly.source,
            analyst=assembly.analyst,
            task_runtime=assembly.runtime,
            checkpointer=assembly.checkpointer,
        )


class DynamicSynthesisGraphFactory(_FrozenGraphFactory):
    """按图 v3 装配动态首轮、协调综合与开放假设持久化。"""

    _graph_version = LEGACY_DYNAMIC_SYNTHESIS_GRAPH_VERSION
    _source_error = "v3 动态综合图需要冻结场景数据源"
    _version_error = "动态综合图只接受当前图版本的 v3 调查"
    _context_error = "v3 动态综合图缺少完整的冻结运行契约"

    def __call__(self, context: ProcessorContext) -> DynamicSynthesisGraph:
        assembly = self._assembly(context)
        return DynamicSynthesisGraph(
            run_id=assembly.run_id,
            planner=assembly.planner,
            policy=assembly.policy,
            observation_cutoff_at=assembly.source.snapshot_time,
            evidence_snapshot_id=assembly.source.snapshot_id,
            data_source=assembly.source,
            analyst=assembly.analyst,
            task_runtime=assembly.runtime,
            synthesizer=assembly.synthesizer,
            checkpointer=assembly.checkpointer,
        )


class DynamicReviewGraphFactory(_FrozenGraphFactory):
    """按图 v4 装配动态首轮、开放假设与独立审查。"""

    _graph_version = LEGACY_DYNAMIC_REVIEW_GRAPH_VERSION
    _source_error = "v3 动态审查图需要冻结场景数据源"
    _version_error = "动态审查图只接受指定图版本的 v3 调查"
    _context_error = "v3 动态审查图缺少完整的冻结运行契约"

    def __call__(self, context: ProcessorContext) -> DynamicReviewGraph:
        assembly = self._assembly(context)
        return DynamicReviewGraph(
            run_id=assembly.run_id,
            planner=assembly.planner,
            policy=assembly.policy,
            observation_cutoff_at=assembly.source.snapshot_time,
            evidence_snapshot_id=assembly.source.snapshot_id,
            data_source=assembly.source,
            analyst=assembly.analyst,
            task_runtime=assembly.runtime,
            synthesizer=assembly.synthesizer,
            reviewer=assembly.reviewer,
            checkpointer=assembly.checkpointer,
        )


class DynamicRoutingGraphFactory(_FrozenGraphFactory):
    """按图 v5 装配动态首轮、独立审查与确定性路由。"""

    _graph_version = LEGACY_DYNAMIC_ROUTING_GRAPH_VERSION
    _source_error = "v3 动态路由图需要冻结场景数据源"
    _version_error = "动态路由图只接受指定图版本的 v3 调查"
    _context_error = "v3 动态路由图缺少完整的冻结运行契约"

    def __call__(self, context: ProcessorContext) -> DynamicRoutingGraph:
        assembly = self._assembly(context)
        return DynamicRoutingGraph(
            run_id=assembly.run_id,
            planner=assembly.planner,
            policy=assembly.policy,
            observation_cutoff_at=assembly.source.snapshot_time,
            evidence_snapshot_id=assembly.source.snapshot_id,
            data_source=assembly.source,
            analyst=assembly.analyst,
            task_runtime=assembly.runtime,
            synthesizer=assembly.synthesizer,
            reviewer=assembly.reviewer,
            router=assembly.router,
            checkpointer=assembly.checkpointer,
        )


class DynamicFollowUpGraphFactory(_FrozenGraphFactory):
    """按图 v6 装配首轮审查路由与第二轮确定性计划。"""

    _graph_version = LEGACY_DYNAMIC_FOLLOW_UP_GRAPH_VERSION
    _source_error = "v3 补查计划图需要冻结场景数据源"
    _version_error = "补查计划图只接受指定图版本的 v3 调查"
    _context_error = "v3 补查计划图缺少完整的冻结运行契约"

    def __call__(self, context: ProcessorContext) -> DynamicFollowUpGraph:
        assembly = self._assembly(context)
        return DynamicFollowUpGraph(
            run_id=assembly.run_id,
            planner=assembly.planner,
            policy=assembly.policy,
            observation_cutoff_at=assembly.source.snapshot_time,
            evidence_snapshot_id=assembly.source.snapshot_id,
            data_source=assembly.source,
            analyst=assembly.analyst,
            task_runtime=assembly.runtime,
            synthesizer=assembly.synthesizer,
            reviewer=assembly.reviewer,
            router=assembly.router,
            follow_up_planner=assembly.follow_up_planner,
            checkpointer=assembly.checkpointer,
        )


class DynamicSecondRoundGraphFactory(_FrozenGraphFactory):
    """按图 v7 装配第二轮计划与专业任务执行。"""

    _graph_version = LEGACY_DYNAMIC_SECOND_ROUND_GRAPH_VERSION
    _source_error = "v3 第二轮执行图需要冻结场景数据源"
    _version_error = "第二轮执行图只接受图 v7 的 v3 调查"
    _context_error = "v3 第二轮执行图缺少完整的冻结运行契约"

    def __call__(self, context: ProcessorContext) -> DynamicSecondRoundGraph:
        assembly = self._assembly(context)
        return DynamicSecondRoundGraph(
            run_id=assembly.run_id,
            planner=assembly.planner,
            policy=assembly.policy,
            observation_cutoff_at=assembly.source.snapshot_time,
            evidence_snapshot_id=assembly.source.snapshot_id,
            data_source=assembly.source,
            analyst=assembly.analyst,
            task_runtime=assembly.runtime,
            synthesizer=assembly.synthesizer,
            reviewer=assembly.reviewer,
            router=assembly.router,
            follow_up_planner=assembly.follow_up_planner,
            checkpointer=assembly.checkpointer,
        )


class DynamicSecondRoundSynthesisGraphFactory(_FrozenGraphFactory):
    """按图 v8 装配第二轮任务执行与新开放假设综合。"""

    _graph_version = LEGACY_DYNAMIC_SECOND_ROUND_SYNTHESIS_GRAPH_VERSION
    _source_error = "v3 第二轮综合图需要冻结场景数据源"
    _version_error = "第二轮综合图只接受图 v8 的 v3 调查"
    _context_error = "v3 第二轮综合图缺少完整的冻结运行契约"

    def __call__(self, context: ProcessorContext) -> DynamicSecondRoundSynthesisGraph:
        assembly = self._assembly(context)
        return DynamicSecondRoundSynthesisGraph(
            run_id=assembly.run_id,
            planner=assembly.planner,
            policy=assembly.policy,
            observation_cutoff_at=assembly.source.snapshot_time,
            evidence_snapshot_id=assembly.source.snapshot_id,
            data_source=assembly.source,
            analyst=assembly.analyst,
            task_runtime=assembly.runtime,
            synthesizer=assembly.synthesizer,
            reviewer=assembly.reviewer,
            router=assembly.router,
            follow_up_planner=assembly.follow_up_planner,
            follow_up_synthesizer=assembly.follow_up_synthesizer,
            checkpointer=assembly.checkpointer,
        )


class DynamicSecondRoundReviewGraphFactory(_FrozenGraphFactory):
    """按图 v9 装配第二轮综合后的独立审查。"""

    _graph_version = LEGACY_DYNAMIC_SECOND_ROUND_REVIEW_GRAPH_VERSION
    _source_error = "v3 第二轮独立审查图需要冻结场景数据源"
    _version_error = "第二轮独立审查图只接受图 v9 的 v3 调查"
    _context_error = "v3 第二轮独立审查图缺少完整的冻结运行契约"

    def __call__(self, context: ProcessorContext) -> DynamicSecondRoundReviewGraph:
        assembly = self._assembly(context)
        return DynamicSecondRoundReviewGraph(
            run_id=assembly.run_id,
            planner=assembly.planner,
            policy=assembly.policy,
            observation_cutoff_at=assembly.source.snapshot_time,
            evidence_snapshot_id=assembly.source.snapshot_id,
            data_source=assembly.source,
            analyst=assembly.analyst,
            task_runtime=assembly.runtime,
            synthesizer=assembly.synthesizer,
            reviewer=assembly.reviewer,
            router=assembly.router,
            follow_up_planner=assembly.follow_up_planner,
            follow_up_synthesizer=assembly.follow_up_synthesizer,
            follow_up_reviewer=assembly.follow_up_reviewer,
            checkpointer=assembly.checkpointer,
        )


class DynamicSecondRoundRoutingGraphFactory(_FrozenGraphFactory):
    """按图 v10 装配第二轮独立审查后的确定性路由。"""

    _graph_version = LEGACY_DYNAMIC_SECOND_ROUND_ROUTING_GRAPH_VERSION
    _source_error = "v3 第二轮确定性路由图需要冻结场景数据源"
    _version_error = "第二轮确定性路由图只接受图 v10 的 v3 调查"
    _context_error = "v3 第二轮确定性路由图缺少完整的冻结运行契约"

    def __call__(self, context: ProcessorContext) -> DynamicSecondRoundRoutingGraph:
        assembly = self._assembly(context)
        return DynamicSecondRoundRoutingGraph(
            run_id=assembly.run_id,
            planner=assembly.planner,
            policy=assembly.policy,
            observation_cutoff_at=assembly.source.snapshot_time,
            evidence_snapshot_id=assembly.source.snapshot_id,
            data_source=assembly.source,
            analyst=assembly.analyst,
            task_runtime=assembly.runtime,
            synthesizer=assembly.synthesizer,
            reviewer=assembly.reviewer,
            router=assembly.router,
            follow_up_planner=assembly.follow_up_planner,
            follow_up_synthesizer=assembly.follow_up_synthesizer,
            follow_up_reviewer=assembly.follow_up_reviewer,
            follow_up_router=assembly.follow_up_router,
            checkpointer=assembly.checkpointer,
        )


class DynamicThirdRoundPlanningGraphFactory(_FrozenGraphFactory):
    """按图 v11 装配第二轮补查路由后的第三轮计划。"""

    _graph_version = LEGACY_DYNAMIC_THIRD_ROUND_PLANNING_GRAPH_VERSION
    _source_error = "v3 第三轮计划图需要冻结场景数据源"
    _version_error = "第三轮计划图只接受图 v11 的 v3 调查"
    _context_error = "v3 第三轮计划图缺少完整的冻结运行契约"

    def __call__(self, context: ProcessorContext) -> DynamicThirdRoundPlanningGraph:
        assembly = self._assembly(context)
        return DynamicThirdRoundPlanningGraph(
            run_id=assembly.run_id,
            planner=assembly.planner,
            policy=assembly.policy,
            observation_cutoff_at=assembly.source.snapshot_time,
            evidence_snapshot_id=assembly.source.snapshot_id,
            data_source=assembly.source,
            analyst=assembly.analyst,
            task_runtime=assembly.runtime,
            synthesizer=assembly.synthesizer,
            reviewer=assembly.reviewer,
            router=assembly.router,
            follow_up_planner=assembly.follow_up_planner,
            follow_up_synthesizer=assembly.follow_up_synthesizer,
            follow_up_reviewer=assembly.follow_up_reviewer,
            follow_up_router=assembly.follow_up_router,
            third_round_planner=assembly.third_round_planner,
            checkpointer=assembly.checkpointer,
        )


class DynamicThirdRoundGraphFactory(_FrozenGraphFactory):
    """按图 v12 装配第三轮专业任务执行。"""

    _graph_version = LEGACY_DYNAMIC_THIRD_ROUND_GRAPH_VERSION
    _source_error = "v3 第三轮专业调查图需要冻结场景数据源"
    _version_error = "第三轮专业调查图只接受图 v12 的 v3 调查"
    _context_error = "v3 第三轮专业调查图缺少完整的冻结运行契约"

    def __call__(self, context: ProcessorContext) -> DynamicThirdRoundGraph:
        assembly = self._assembly(context)
        return DynamicThirdRoundGraph(
            run_id=assembly.run_id,
            planner=assembly.planner,
            policy=assembly.policy,
            observation_cutoff_at=assembly.source.snapshot_time,
            evidence_snapshot_id=assembly.source.snapshot_id,
            data_source=assembly.source,
            analyst=assembly.analyst,
            task_runtime=assembly.runtime,
            synthesizer=assembly.synthesizer,
            reviewer=assembly.reviewer,
            router=assembly.router,
            follow_up_planner=assembly.follow_up_planner,
            follow_up_synthesizer=assembly.follow_up_synthesizer,
            follow_up_reviewer=assembly.follow_up_reviewer,
            follow_up_router=assembly.follow_up_router,
            third_round_planner=assembly.third_round_planner,
            checkpointer=assembly.checkpointer,
        )


class DynamicThirdRoundSynthesisGraphFactory(_FrozenGraphFactory):
    """按图 v13 装配第三轮综合与开放假设恢复。"""

    _graph_version = LEGACY_DYNAMIC_THIRD_ROUND_SYNTHESIS_GRAPH_VERSION
    _source_error = "v3 第三轮综合图需要冻结场景数据源"
    _version_error = "第三轮综合图只接受图 v13 的 v3 调查"
    _context_error = "v3 第三轮综合图缺少完整的冻结运行契约"

    def __call__(self, context: ProcessorContext) -> DynamicThirdRoundSynthesisGraph:
        assembly = self._assembly(context)
        return DynamicThirdRoundSynthesisGraph(
            run_id=assembly.run_id,
            planner=assembly.planner,
            policy=assembly.policy,
            observation_cutoff_at=assembly.source.snapshot_time,
            evidence_snapshot_id=assembly.source.snapshot_id,
            data_source=assembly.source,
            analyst=assembly.analyst,
            task_runtime=assembly.runtime,
            synthesizer=assembly.synthesizer,
            reviewer=assembly.reviewer,
            router=assembly.router,
            follow_up_planner=assembly.follow_up_planner,
            follow_up_synthesizer=assembly.follow_up_synthesizer,
            follow_up_reviewer=assembly.follow_up_reviewer,
            follow_up_router=assembly.follow_up_router,
            third_round_planner=assembly.third_round_planner,
            third_round_synthesizer=assembly.third_round_synthesizer,
            checkpointer=assembly.checkpointer,
        )


class DynamicThirdRoundReviewGraphFactory(_FrozenGraphFactory):
    """按图 v14 装配第三轮独立审查与恢复。"""

    _graph_version = LEGACY_DYNAMIC_THIRD_ROUND_REVIEW_GRAPH_VERSION
    _source_error = "v3 第三轮审查图需要冻结场景数据源"
    _version_error = "第三轮审查图只接受图 v14 的 v3 调查"
    _context_error = "v3 第三轮审查图缺少完整的冻结运行契约"

    def __call__(self, context: ProcessorContext) -> DynamicThirdRoundReviewGraph:
        assembly = self._assembly(context)
        return DynamicThirdRoundReviewGraph(
            run_id=assembly.run_id,
            planner=assembly.planner,
            policy=assembly.policy,
            observation_cutoff_at=assembly.source.snapshot_time,
            evidence_snapshot_id=assembly.source.snapshot_id,
            data_source=assembly.source,
            analyst=assembly.analyst,
            task_runtime=assembly.runtime,
            synthesizer=assembly.synthesizer,
            reviewer=assembly.reviewer,
            router=assembly.router,
            follow_up_planner=assembly.follow_up_planner,
            follow_up_synthesizer=assembly.follow_up_synthesizer,
            follow_up_reviewer=assembly.follow_up_reviewer,
            follow_up_router=assembly.follow_up_router,
            third_round_planner=assembly.third_round_planner,
            third_round_synthesizer=assembly.third_round_synthesizer,
            third_round_reviewer=assembly.third_round_reviewer,
            checkpointer=assembly.checkpointer,
        )
