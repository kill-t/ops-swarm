"""将固定图的一次专业操作绑定到持久任务生命周期。"""

from collections.abc import Awaitable, Callable, Sequence
from contextlib import nullcontext
from contextvars import ContextVar
from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID

from opsswarm.investigation.budget.contracts import CallOperation
from opsswarm.investigation.budget.runtime import BudgetedCallRunner
from opsswarm.investigation.models import (
    EvidenceKind,
    EvidenceObservation,
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
from opsswarm.investigation.tasks.evidence_store import TaskEvidenceStore
from opsswarm.investigation.tasks.queries import CollaborationQueries
from opsswarm.investigation.tasks.records import TaskRecord
from opsswarm.investigation.tasks.results import SpecialistTaskResult
from opsswarm.investigation.tasks.store import InvestigationTaskStore


class _PersistentTaskRuntime:
    """共享任务尝试、证据、预算和结果恢复，不决定任务如何产生。"""

    def __init__(
        self,
        *,
        run_id: UUID,
        lease_token: UUID,
        store: InvestigationTaskStore,
        queries: CollaborationQueries,
        snapshot_id: str,
        snapshot_time: datetime,
        data_source_mode: Literal["frozen", "live"] = "frozen",
        legacy_snapshot_ids: frozenset[str] = frozenset(),
        call_runner: BudgetedCallRunner | None = None,
        model_input_budget: bool = False,
        evidence_store: TaskEvidenceStore | None = None,
    ) -> None:
        self.run_id = run_id
        self.snapshot_id = snapshot_id
        self.data_source_mode = data_source_mode
        self._legacy_snapshot_ids = (
            legacy_snapshot_ids if data_source_mode == "frozen" else frozenset()
        )
        self._snapshot_time = snapshot_time
        self._lease_token = lease_token
        self._store = store
        self._queries = queries
        self._call_runner = call_runner
        self._model_input_budget = model_input_budget
        self._evidence_store = evidence_store
        self._scope: ContextVar[tuple[UUID, UUID] | None] = ContextVar(
            "task_evidence_scope", default=None
        )

    async def _prepare_fixed(self, request: InvestigationRequest) -> None:
        await self.require_active()
        if self._legacy_snapshot_ids:
            existing = await self._queries.get_snapshot(self.run_id)
            ids = {t.assignment.evidence_snapshot_id for r in existing.rounds for t in r.tasks}
            if len(ids) == 1:
                saved_id = next(iter(ids))
                if saved_id is not None and saved_id in self._legacy_snapshot_ids:
                    # 只沿用已经持久化的身份，完整分派仍由 create_round 在租约事务中核对。
                    self.snapshot_id = saved_id
        await self._store.create_round(
            self.run_id,
            lease_token=self._lease_token,
            assignment=RoundAssignment(
                round_number=1,
                goal="完成三类冻结证据的固定并行调查"
                if self.data_source_mode == "frozen"
                else "完成固定观测窗口内的三类现场证据调查",
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

    async def call_tool(
        self, role: EvidenceKind, query: Callable[[], Awaitable[Sequence[EvidenceObservation]]]
    ) -> Sequence[EvidenceObservation]:
        scope = self._scope.get()
        if self._evidence_store is not None:
            if scope is None or self._call_runner is None:
                raise TaskFactConflict("task_evidence_scope_missing", "取证缺少任务身份或预算入口")
            stored = await self._evidence_store.read_for_attempt(
                self.run_id, scope[0], scope[1], lease_token=self._lease_token
            )
            if stored is not None:
                await self.require_active()
                return stored.snapshot.evidence
        operation = {
            EvidenceKind.LOG: CallOperation.QUERY_LOGS,
            EvidenceKind.METRIC: CallOperation.QUERY_METRICS,
            EvidenceKind.CHANGE: CallOperation.LIST_CHANGES,
        }[role]
        if self._call_runner is None:
            return await query()
        evidence = await self._call_runner.execute(operation, query)
        if self._evidence_store is not None and scope is not None:
            saved = await self._evidence_store.save(
                self.run_id, scope[0], scope[1], lease_token=self._lease_token, evidence=evidence
            )
            return saved.snapshot.evidence
        return evidence

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

    async def _completed_result(self, task: TaskRecord) -> SpecialistTaskResult:
        stored = await self._queries.get_result(self.run_id, task.id)
        if stored is None or stored.result.evidence_snapshot_id != self.snapshot_id:
            raise TaskFactConflict("task_result_missing", "已成功任务缺少匹配的专业快照")
        if self._evidence_store is not None:
            await self._evidence_store.validate_result(
                self.run_id, task.id, stored.result, lease_token=self._lease_token
            )
        return stored.result

    async def _execute_task(
        self,
        task: TaskRecord,
        operation: Callable[[], Awaitable[SpecialistTaskResult]],
    ) -> SpecialistTaskResult:
        if task.status is TaskStatus.SUCCEEDED:
            return await self._completed_result(task)
        started = await self._store.begin_attempt(
            self.run_id,
            task.id,
            lease_token=self._lease_token,
            expected_version=task.version,
        )
        if not started.created or started.attempt is None:
            raise TaskFactConflict("task_attempt_not_acquired", "未取得新的专业执行尝试")
        token = self._scope.set((task.id, started.attempt.id))
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
        finally:
            self._scope.reset(token)
        await self._store.finish_attempt(
            self.run_id,
            task.id,
            started.attempt.id,
            lease_token=self._lease_token,
            outcome=TaskOutcome(status="succeeded", summary=result.report.summary),
            result=result,
        )
        return result


class FixedTaskRuntime(_PersistentTaskRuntime):
    """固定图创建并执行日志、指标和变更三个任务。"""

    async def prepare(self, request: InvestigationRequest) -> None:
        await self._prepare_fixed(request)

    async def run(
        self,
        role: EvidenceKind,
        operation: Callable[[], Awaitable[SpecialistTaskResult]],
    ) -> SpecialistTaskResult:
        await self.require_active()
        snapshot = await self._queries.get_snapshot(self.run_id)
        if len(snapshot.rounds) != 1:
            raise TaskFactConflict("task_round_mismatch", "固定图必须绑定唯一轮次")
        matches = [item for item in snapshot.rounds[0].tasks if item.assignment.role is role]
        if len(matches) != 1:
            raise TaskFactConflict("task_assignment_mismatch", "固定图缺少唯一专业任务")
        return await self._execute_task(matches[0], operation)

    async def validate_completed(self, artifacts: InvestigationArtifacts) -> None:
        await self.require_active()
        await self._store.validate_completed(
            self.run_id,
            lease_token=self._lease_token,
            artifacts=artifacts,
        )


class DynamicTaskRuntime(_PersistentTaskRuntime):
    """只执行协调计划已创建的任务，不生成或扩大分派范围。"""

    async def run(
        self,
        *,
        round_id: UUID,
        expected_round_number: int,
        assignment: TaskAssignment,
        operation: Callable[[], Awaitable[SpecialistTaskResult]],
    ) -> SpecialistTaskResult:
        await self.require_active()
        task = await self._planned_task(round_id, expected_round_number, assignment)
        return await self._execute_task(task, operation)

    async def get_completed(
        self,
        *,
        round_id: UUID,
        expected_round_number: int,
        assignment: TaskAssignment,
    ) -> SpecialistTaskResult:
        """最终返回前从业务库复核已成功结果、证据与摘要。"""

        await self.require_active()
        task = await self._planned_task(round_id, expected_round_number, assignment)
        if task.status is not TaskStatus.SUCCEEDED:
            raise TaskFactConflict("task_result_missing", "动态任务尚未成功")
        return await self._completed_result(task)

    async def _planned_task(
        self,
        round_id: UUID,
        expected_round_number: int,
        assignment: TaskAssignment,
    ) -> TaskRecord:
        if assignment.evidence_snapshot_id != self.snapshot_id:
            raise TaskFactConflict("task_assignment_mismatch", "动态任务不属于当前冻结快照")
        snapshot = await self._queries.get_snapshot(self.run_id)
        rounds = [item for item in snapshot.rounds if item.id == round_id]
        if len(rounds) != 1 or rounds[0].round_number != expected_round_number:
            raise TaskFactConflict("task_round_mismatch", "动态任务缺少匹配的业务轮次")
        tasks = [
            item for item in rounds[0].tasks if item.assignment.task_key == assignment.task_key
        ]
        if len(tasks) != 1 or tasks[0].assignment != assignment:
            raise TaskFactConflict("task_assignment_mismatch", "动态任务与持久计划不一致")
        return tasks[0]
