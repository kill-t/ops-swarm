"""将固定图的一次专业操作绑定到持久任务生命周期。"""

from collections.abc import Awaitable, Callable
from contextlib import nullcontext
from datetime import datetime, timedelta
from uuid import UUID

from opsswarm.investigation.budget.contracts import CallOperation
from opsswarm.investigation.budget.runtime import BudgetedCallRunner
from opsswarm.investigation.models import (
    EvidenceKind,
    InvestigationArtifacts,
    InvestigationRequest,
)
from opsswarm.investigation.tasks.contracts import (
    RoundAssignment,
    TaskAssignment,
    TaskOutcome,
    TaskStatus,
)
from opsswarm.investigation.tasks.errors import TaskFactConflict
from opsswarm.investigation.tasks.queries import CollaborationQueries
from opsswarm.investigation.tasks.results import SpecialistTaskResult
from opsswarm.investigation.tasks.store import InvestigationTaskStore


class FixedTaskRuntime:
    """只组织任务开始、复用和结算；不包含 SQL、模型提示或网络适配。"""

    def __init__(
        self,
        *,
        run_id: UUID,
        lease_token: UUID,
        store: InvestigationTaskStore,
        queries: CollaborationQueries,
        snapshot_id: str,
        snapshot_time: datetime,
        call_runner: BudgetedCallRunner | None = None,
        model_input_budget: bool = False,
    ) -> None:
        self.run_id = run_id
        self.snapshot_id = snapshot_id
        self._snapshot_time = snapshot_time
        self._lease_token = lease_token
        self._store = store
        self._queries = queries
        self._call_runner = call_runner
        self._model_input_budget = model_input_budget

    async def prepare(self, request: InvestigationRequest) -> None:
        await self._store.create_round(
            self.run_id,
            lease_token=self._lease_token,
            assignment=RoundAssignment(
                round_number=1,
                goal="完成三类冻结证据的固定并行调查",
                tasks=tuple(
                    TaskAssignment(
                        task_key=role.value,
                        role=role,
                        goal=f"分析 {role.value} 专业证据",
                        service_name=request.service_name,
                        window_end=self._snapshot_time,
                        window_start=self._snapshot_time
                        - (
                            timedelta(hours=6)
                            if role is EvidenceKind.CHANGE
                            else timedelta(minutes=15)
                        ),
                        evidence_snapshot_id=self.snapshot_id,
                    )
                    for role in EvidenceKind
                ),
            ),
        )

    async def require_active(self) -> None:
        await self._store.require_active(self.run_id, lease_token=self._lease_token)
        if self._call_runner is not None:
            await self._call_runner.check()

    async def call_tool[T](self, role: EvidenceKind, query: Callable[[], Awaitable[T]]) -> T:
        operation = {
            EvidenceKind.LOG: CallOperation.QUERY_LOGS,
            EvidenceKind.METRIC: CallOperation.QUERY_METRICS,
            EvidenceKind.CHANGE: CallOperation.LIST_CHANGES,
        }[role]
        if self._call_runner is None:
            return await query()
        return await self._call_runner.execute(operation, query)

    async def call_model[T](
        self, role: EvidenceKind | None, invoke: Callable[[], Awaitable[T]]
    ) -> T:
        # 新路径在适配器组装完整输入后原子预留次数与 Token，不能在图外层重复占用。
        if self._model_input_budget:
            return await invoke()
        operation = (
            CallOperation(f"analyze_{role.value}") if role is not None else CallOperation.SYNTHESIZE
        )
        if self._call_runner is None:
            return await invoke()
        return await self._call_runner.execute(operation, invoke)

    async def run(
        self,
        role: EvidenceKind,
        operation: Callable[[], Awaitable[SpecialistTaskResult]],
    ) -> SpecialistTaskResult:
        await self.require_active()
        snapshot = await self._queries.get_snapshot(self.run_id)
        if len(snapshot.rounds) != 1:
            raise TaskFactConflict("task_round_mismatch", "固定图必须绑定唯一轮次")
        task = next(item for item in snapshot.rounds[0].tasks if item.assignment.role is role)
        if task.status is TaskStatus.SUCCEEDED:
            stored = await self._queries.get_result(self.run_id, task.id)
            if stored is None or stored.result.evidence_snapshot_id != self.snapshot_id:
                raise TaskFactConflict("task_result_missing", "已成功任务缺少匹配的专业快照")
            return stored.result
        started = await self._store.begin_attempt(
            self.run_id,
            task.id,
            lease_token=self._lease_token,
            expected_version=task.version,
        )
        if not started.created or started.attempt is None:
            raise TaskFactConflict("task_attempt_not_acquired", "未取得新的专业执行尝试")
        try:
            with (
                self._call_runner.task_scope(task.id, started.attempt.id)
                if self._call_runner
                else nullcontext()
            ):
                result = await operation()
        except Exception:
            await self._store.finish_attempt(
                self.run_id,
                task.id,
                started.attempt.id,
                lease_token=self._lease_token,
                outcome=TaskOutcome(
                    status="failed", error_code="specialist_operation_failed", retryable=True
                ),
            )
            raise
        await self._store.finish_attempt(
            self.run_id,
            task.id,
            started.attempt.id,
            lease_token=self._lease_token,
            outcome=TaskOutcome(status="succeeded", summary=result.report.summary),
            result=result,
        )
        return result

    async def validate_completed(self, artifacts: InvestigationArtifacts) -> None:
        await self.require_active()
        await self._store.validate_completed(
            self.run_id,
            lease_token=self._lease_token,
            artifacts=artifacts,
        )
