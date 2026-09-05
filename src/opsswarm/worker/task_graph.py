"""按父尝试装配独立的 v2 图与任务运行器。"""

from collections.abc import Callable
from datetime import datetime

from langgraph.checkpoint.base import BaseCheckpointSaver
from opentelemetry.trace import Tracer
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.investigation.budget.runtime import BudgetedCallRunner
from opsswarm.investigation.budget.store import CallBudgetStore
from opsswarm.investigation.parallel_graph import (
    ConclusionSynthesizer,
    LangGraphParallelInvestigator,
    SpecialistAnalyst,
)
from opsswarm.investigation.scenarios import FrozenScenarioDataSource
from opsswarm.investigation.tasks.queries import CollaborationQueries
from opsswarm.investigation.tasks.runtime import FixedTaskRuntime
from opsswarm.investigation.tasks.store import InvestigationTaskStore, utc_now
from opsswarm.investigation.tools import InvestigationDataSource
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
    ) -> None:
        self._source = data_source
        self._analyst = analyst
        self._synthesizer = synthesizer
        self._checkpointer = checkpointer
        self._tracer = tracer
        self._store = InvestigationTaskStore(session_factory, clock=clock)
        self._queries = CollaborationQueries(session_factory)
        self._budget_store = CallBudgetStore(session_factory, clock=clock)

    def __call__(self, context: ProcessorContext) -> LangGraphParallelInvestigator:
        if context.investigation_run_id is None or context.lease_token is None:
            raise ValueError("v2 调查需要系统注入运行与租约身份")
        if not isinstance(self._source, FrozenScenarioDataSource):
            raise ValueError("v2 固定图只接受冻结场景")
        return LangGraphParallelInvestigator(
            data_source=self._source,
            analyst=self._analyst,
            synthesizer=self._synthesizer,
            checkpointer=self._checkpointer,
            tracer=self._tracer,
            task_runtime=FixedTaskRuntime(
                run_id=context.investigation_run_id,
                lease_token=context.lease_token,
                store=self._store,
                queries=self._queries,
                snapshot_id=self._source.snapshot_id,
                snapshot_time=self._source.snapshot_time,
                call_runner=BudgetedCallRunner(
                    run_id=context.investigation_run_id,
                    lease_token=context.lease_token,
                    store=self._budget_store,
                )
                if context.call_budget is not None
                else None,
            ),
        )
