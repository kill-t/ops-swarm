"""用持久预留约束一次受限操作，隔离并发专业调用身份。"""

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from uuid import UUID

from opsswarm.investigation.budget.contracts import CallOperation
from opsswarm.investigation.budget.model_contracts import ModelInput, ReportedModelUsage
from opsswarm.investigation.budget.policy import CallBudgetExceeded
from opsswarm.investigation.budget.store import CallBudgetStore
from opsswarm.investigation.context.contracts import ContextManifest
from opsswarm.investigation.tasks.errors import TaskFactConflict


class BudgetedCallRunner:
    def __init__(self, *, run_id: UUID, lease_token: UUID, store: CallBudgetStore) -> None:
        self._run_id = run_id
        self._lease_token = lease_token
        self._store = store
        self._scope: ContextVar[tuple[UUID, UUID] | None] = ContextVar(
            "professional_call_scope", default=None
        )

    @contextmanager
    def task_scope(self, task_id: UUID, attempt_id: UUID) -> Iterator[None]:
        token = self._scope.set((task_id, attempt_id))
        try:
            yield
        finally:
            self._scope.reset(token)

    @property
    def current_task_id(self) -> UUID | None:
        scope = self._scope.get()
        return scope[0] if scope else None

    async def check(self) -> None:
        await self._store.check(self._run_id, lease_token=self._lease_token)

    async def execute[T](
        self,
        operation: CallOperation,
        invoke: Callable[[], Awaitable[T]],
        *,
        model_input: ModelInput | None = None,
        prompt_version: str | None = None,
        context_manifest: ContextManifest | None = None,
        invocation_index: int = 0,
        usage_reader: Callable[[], ReportedModelUsage | None] = lambda: None,
    ) -> T:
        scope = self._scope.get()
        grant = await self._store.reserve(
            self._run_id,
            lease_token=self._lease_token,
            operation=operation,
            task_id=scope[0] if scope else None,
            task_attempt_id=scope[1] if scope else None,
            model_input=model_input,
            prompt_version=prompt_version,
            context_manifest=context_manifest,
            invocation_index=invocation_index,
        )
        if not grant.created:
            raise TaskFactConflict("call_already_reserved", "相同预留不能再次执行操作")
        remaining = await self._store.check(self._run_id, lease_token=self._lease_token)
        timer = asyncio.timeout(remaining)
        try:
            async with timer:
                result = await invoke()
        except TimeoutError:
            await self._store.settle(
                self._run_id,
                grant.record.id,
                lease_token=self._lease_token,
                status="unknown",
                error_code="run_deadline_exceeded" if timer.expired() else "operation_failed",
                model_usage=usage_reader(),
            )
            if timer.expired():
                raise CallBudgetExceeded("run_deadline_exceeded") from None
            raise
        except asyncio.CancelledError:
            # 如果进程来不及提交，父尝试收尾仍会将 reserved 记为 unknown。
            await self._store.settle(
                self._run_id,
                grant.record.id,
                lease_token=self._lease_token,
                status="unknown",
                error_code="operation_cancelled",
                model_usage=usage_reader(),
            )
            raise
        except Exception:
            await self._store.settle(
                self._run_id,
                grant.record.id,
                lease_token=self._lease_token,
                status="failed",
                error_code="operation_failed",
                model_usage=usage_reader(),
            )
            await self.check()
            raise
        await self._store.settle(
            self._run_id,
            grant.record.id,
            lease_token=self._lease_token,
            status="succeeded",
            model_usage=usage_reader(),
        )
        await self.check()
        return result
