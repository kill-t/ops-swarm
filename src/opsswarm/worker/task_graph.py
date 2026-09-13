"""按父尝试装配独立的 v2 图与任务运行器。"""

from collections.abc import Callable
from datetime import datetime

from langchain_openai import ChatOpenAI
from langgraph.checkpoint.base import BaseCheckpointSaver
from opentelemetry.trace import Tracer
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.investigation.budget.model_caller import BudgetedModelCaller
from opsswarm.investigation.budget.model_contracts import ModelProfile
from opsswarm.investigation.budget.policy import CallBudgetExceeded
from opsswarm.investigation.budget.runtime import BudgetedCallRunner
from opsswarm.investigation.budget.store import CallBudgetStore
from opsswarm.investigation.context.service import TaskContextService
from opsswarm.investigation.live import LiveInvestigationDataSource
from opsswarm.investigation.live_window import UnsupportedLiveTaskSource
from opsswarm.investigation.loop.agent import EvidenceLoopAnalyst
from opsswarm.investigation.loop.service import EvidenceLoopService
from opsswarm.investigation.models import InvestigationDataSourceMode
from opsswarm.investigation.parallel_graph import (
    ConclusionSynthesizer,
    LangGraphParallelInvestigator,
    SpecialistAnalyst,
)
from opsswarm.investigation.parallel_model import (
    LangChainConclusionSynthesizer,
    LangChainSpecialistAnalyst,
)
from opsswarm.investigation.query_execution import ReadOnlyQueryExecutor
from opsswarm.investigation.scenarios import FrozenScenarioDataSource
from opsswarm.investigation.tasks.evidence import live_snapshot_id
from opsswarm.investigation.tasks.evidence_store import TaskEvidenceStore
from opsswarm.investigation.tasks.queries import CollaborationQueries
from opsswarm.investigation.tasks.runtime import FixedTaskRuntime
from opsswarm.investigation.tasks.store import InvestigationTaskStore, utc_now
from opsswarm.investigation.tools import InvestigationDataSource
from opsswarm.investigation.workflow_versions import WorkflowVersion
from opsswarm.worker.runner import ProcessorContext


class TaskGraphFactory:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        data_source: InvestigationDataSource,
        analyst: SpecialistAnalyst,
        synthesizer: ConclusionSynthesizer,
        checkpointer: BaseCheckpointSaver[str],
        tracer: Tracer | None = None,
        clock: Callable[[], datetime] = utc_now,
        budget_model: ChatOpenAI | None = None,
        model_profile: ModelProfile | None = None,
        query_executor: ReadOnlyQueryExecutor | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._source = data_source
        self._analyst = analyst
        self._synthesizer = synthesizer
        self._checkpointer = checkpointer
        self._tracer = tracer
        self._store = InvestigationTaskStore(session_factory, clock=clock)
        self._queries = CollaborationQueries(session_factory)
        self._budget_store = CallBudgetStore(session_factory, clock=clock)
        self._budget_model = budget_model
        self._model_profile = model_profile
        self._query_executor = query_executor
        self._evidence_store = TaskEvidenceStore(session_factory, clock=clock)

    def __call__(self, context: ProcessorContext) -> LangGraphParallelInvestigator:
        if context.workflow_version is not WorkflowVersion.V2:
            raise ValueError("固定任务图只能装配 v2 调查")
        if context.investigation_run_id is None or context.lease_token is None:
            raise ValueError("v2 调查需要系统注入运行与租约身份")
        source: InvestigationDataSource = self._source
        if context.data_source_mode is InvestigationDataSourceMode.LIVE:
            if (
                not isinstance(self._source, LiveInvestigationDataSource)
                or context.observation_cutoff_at is None
                or context.evidence_policy_version != "task-evidence-live-v1"
                or context.call_budget is None
            ):
                raise UnsupportedLiveTaskSource("现场 v2 缺少匹配的来源、窗口或取证预算策略")
            snapshot_time = context.observation_cutoff_at
            snapshot_id = live_snapshot_id(context.investigation_run_id, snapshot_time)
            source = self._source.bind_window(
                service_name=context.request.service_name, cutoff=snapshot_time
            )
        else:
            if (
                not isinstance(self._source, FrozenScenarioDataSource)
                or context.observation_cutoff_at is not None
                or context.evidence_policy_version not in (None, "task-evidence-v1")
            ):
                raise ValueError("冻结 v2 缺少匹配的冻结数据源或取证策略")
            snapshot_id, snapshot_time = self._source.snapshot_id, self._source.snapshot_time
        runner = (
            BudgetedCallRunner(
                run_id=context.investigation_run_id,
                lease_token=context.lease_token,
                store=self._budget_store,
            )
            if context.call_budget is not None
            else None
        )
        analyst, synthesizer = self._analyst, self._synthesizer
        if context.model_budget is not None:
            model, profile = self._budget_model, self._model_profile
            if (
                runner is None
                or model is None
                or profile is None
                or profile != context.model_budget.profile
                or model.model_name != profile.model_name
                or model.max_retries != 0
                or model.n != 1
            ):
                raise CallBudgetExceeded("model_budget_profile_mismatch")
            caller = BudgetedModelCaller(
                model,
                context.model_budget,
                runner,
                context_service=TaskContextService(
                    self._session_factory, context.investigation_run_id
                )
                if context.context_policy_version is not None
                else None,
            )
            analyst = LangChainSpecialistAnalyst(
                model,
                structured_output_method=profile.structured_output_method,
                model_caller=caller,
            )
            if context.context_policy_version in ("evidence-read-v1", "summary-context-v1"):
                if profile.structured_output_method != "function_calling":
                    raise CallBudgetExceeded("model_budget_profile_mismatch")
                analyst = EvidenceLoopAnalyst(
                    model,
                    policy=context.model_budget,
                    runner=runner,
                    service=EvidenceLoopService(
                        self._session_factory, context.investigation_run_id
                    ),
                )
            synthesizer = LangChainConclusionSynthesizer(
                model,
                structured_output_method=profile.structured_output_method,
                model_caller=caller,
            )
        return LangGraphParallelInvestigator(
            data_source=source,
            query_executor=self._query_executor,
            analyst=analyst,
            synthesizer=synthesizer,
            checkpointer=self._checkpointer,
            tracer=self._tracer,
            task_runtime=FixedTaskRuntime(
                run_id=context.investigation_run_id,
                lease_token=context.lease_token,
                store=self._store,
                queries=self._queries,
                snapshot_id=snapshot_id,
                snapshot_time=snapshot_time,
                data_source_mode=context.data_source_mode.value,
                legacy_snapshot_ids=self._source.legacy_snapshot_ids
                if isinstance(self._source, FrozenScenarioDataSource)
                else frozenset(),
                call_runner=runner,
                model_input_budget=context.model_budget is not None,
                evidence_store=self._evidence_store
                if context.evidence_policy_version is not None
                else None,
            ),
        )
