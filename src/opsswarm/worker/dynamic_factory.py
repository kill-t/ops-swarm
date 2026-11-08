"""为当前动态Worker按持久来源装配冻结或现场调查图。"""

from collections.abc import Callable
from datetime import datetime

from langchain_openai import ChatOpenAI
from langgraph.checkpoint.base import BaseCheckpointSaver
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.investigation.budget.policy import CallBudgetExceeded
from opsswarm.investigation.budget.runtime import BudgetedCallRunner
from opsswarm.investigation.budget.store import CallBudgetStore
from opsswarm.investigation.coordination.planner import CoordinationPlanner
from opsswarm.investigation.dynamic_live_source import DynamicLiveDataSource
from opsswarm.investigation.dynamic_third_round_routing_graph import DynamicThirdRoundRoutingGraph
from opsswarm.investigation.follow_up_planning.service import (
    FollowUpPlanService,
    ThirdRoundPlanService,
)
from opsswarm.investigation.graph_thread import build_graph_thread_id
from opsswarm.investigation.live import LiveInvestigationDataSource
from opsswarm.investigation.loop.agent import EvidenceLoopAnalyst
from opsswarm.investigation.loop.service import EvidenceLoopService
from opsswarm.investigation.models import (
    InvestigationDataSourceMode,
    InvestigationMode,
    InvestigationRequest,
)
from opsswarm.investigation.query_execution import ReadOnlyQueryExecutor
from opsswarm.investigation.review.service import (
    IndependentHypothesisReviewer,
    SecondRoundHypothesisReviewer,
    ThirdRoundHypothesisReviewer,
)
from opsswarm.investigation.review_routing.service import (
    ReviewRoutingService,
    SecondRoundReviewRoutingService,
    ThirdRoundReviewRoutingService,
)
from opsswarm.investigation.scenarios import RELEASE_500_SCENARIO, FrozenScenarioDataSource
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
    CURRENT_DYNAMIC_GRAPH_VERSIONS,
    WorkflowVersion,
    is_supported_dynamic_source,
)
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


class DynamicThirdRoundRoutingGraphFactory:
    """按持久来源装配v15冻结或v16现场的有界多轮调查。"""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        data_source: FrozenScenarioDataSource | LiveInvestigationDataSource,
        model: ChatOpenAI,
        checkpointer: BaseCheckpointSaver[str],
        clock: Callable[[], datetime] = utc_now,
        query_executor: ReadOnlyQueryExecutor | None = None,
    ) -> None:
        if not isinstance(data_source, (FrozenScenarioDataSource, LiveInvestigationDataSource)):
            raise ValueError("动态停止路由图需要明确的冻结或现场来源")
        self._session_factory = session_factory
        self._source = data_source
        self._model = model
        self._checkpointer = checkpointer
        self._clock = clock
        self._query_executor = query_executor

    def __call__(self, context: ProcessorContext) -> DynamicThirdRoundRoutingGraph:
        if (
            context.workflow_version is not WorkflowVersion.V3
            or context.graph_version not in CURRENT_DYNAMIC_GRAPH_VERSIONS
        ):
            raise ValueError("第三轮停止路由图只接受当前图版本的 v3 调查")
        if (
            context.mode is not InvestigationMode.MULTI_AGENT
            or not is_supported_dynamic_source(
                context.graph_version,
                context.data_source_mode.value,
                context.evidence_policy_version,
            )
            or context.investigation_run_id is None
            or context.lease_token is None
            or context.graph_thread_id is None
            or context.graph_thread_id != build_graph_thread_id(context.investigation_run_id)
            or context.coordination_policy is None
            or context.call_budget is None
            or context.model_budget is None
            or context.context_policy_version != "summary-context-v1"
        ):
            raise ValueError("v3 第三轮停止路由图缺少完整的来源运行契约")
        live = context.data_source_mode is InvestigationDataSourceMode.LIVE
        source: FrozenScenarioDataSource | DynamicLiveDataSource
        if live:
            if (
                not isinstance(self._source, LiveInvestigationDataSource)
                or context.observation_cutoff_at is None
                or self._query_executor is None
            ):
                raise ValueError("现场动态图缺少固定截止、MCP来源或受限执行器")
            source = self._source.bind_dynamic_window(
                run_id=context.investigation_run_id,
                service_name=context.request.service_name,
                cutoff=context.observation_cutoff_at,
                executor=self._query_executor,
            )
        else:
            if (
                not isinstance(self._source, FrozenScenarioDataSource)
                or context.observation_cutoff_at is not None
            ):
                raise ValueError("冻结动态图不能替换为现场来源或观测窗口")
            source = self._source
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
            snapshot_id=source.snapshot_id,
            snapshot_time=source.snapshot_time,
            data_source_mode="live" if live else "frozen",
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
        return DynamicThirdRoundRoutingGraph(
            run_id=context.investigation_run_id,
            planner=CoordinationPlanner(
                self._session_factory,
                run_id=context.investigation_run_id,
                lease_token=context.lease_token,
                model=self._model,
                clock=self._clock,
            ),
            policy=context.coordination_policy,
            observation_cutoff_at=source.snapshot_time,
            evidence_snapshot_id=source.snapshot_id,
            data_source=source,
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
            third_round_router=ThirdRoundReviewRoutingService(
                self._session_factory,
                run_id=context.investigation_run_id,
                lease_token=context.lease_token,
                clock=self._clock,
            ),
            checkpointer=self._checkpointer,
            graph_version=context.graph_version,
        )
