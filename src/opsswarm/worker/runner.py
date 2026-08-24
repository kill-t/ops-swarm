"""单次领取并处理持久调查任务的 Worker。"""

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Protocol

from opsswarm.investigation.models import (
    InvestigationMetrics,
    InvestigationMode,
    InvestigationRequest,
    InvestigationRunResult,
    StrictModel,
    WorkItemError,
    WorkItemKind,
)
from opsswarm.investigation.persistence import InvestigationStore


class ProcessorOutcome(StrictModel):
    """处理器交给持久化层的结构化成功结果。"""

    result: InvestigationRunResult
    metrics: InvestigationMetrics
    model_name: str


class ProcessorContext(StrictModel):
    """Worker 交给处理器的持久路由上下文。"""

    request: InvestigationRequest
    mode: InvestigationMode
    work_item_kind: WorkItemKind
    graph_thread_id: str | None


class InvestigationProcessor(Protocol):
    """隔离 LangChain、假模型和未来 LangGraph 处理器。"""

    async def process(self, context: ProcessorContext) -> ProcessorOutcome:
        """调查一个事故并返回可持久化结果。"""


class InvestigationWorker:
    """通过租约运行一个任务，不在进程内维护队列状态。"""

    def __init__(
        self,
        *,
        store: InvestigationStore,
        processor: InvestigationProcessor,
        worker_id: str,
        clock: Callable[[], datetime],
        lease_duration: timedelta = timedelta(minutes=2),
        base_retry_delay: timedelta = timedelta(seconds=5),
        max_retry_delay: timedelta = timedelta(minutes=5),
    ) -> None:
        if base_retry_delay < timedelta(0):
            raise ValueError("base_retry_delay 不能小于 0")
        if max_retry_delay < base_retry_delay:
            raise ValueError("max_retry_delay 不能小于 base_retry_delay")
        self._store = store
        self._processor = processor
        self._worker_id = worker_id
        self._clock = clock
        self._lease_duration = lease_duration
        self._base_retry_delay = base_retry_delay
        self._max_retry_delay = max_retry_delay

    async def run_once(self) -> bool:
        """领取至多一个任务；无可用任务返回 False。"""

        claimed = await self._store.claim_next(
            worker_id=self._worker_id,
            occurred_at=self._clock(),
            lease_duration=self._lease_duration,
        )
        if claimed is None:
            return False
        lease_token = claimed.work_item.lease_token
        if lease_token is None:
            raise RuntimeError("已领取任务缺少 lease_token")

        try:
            outcome = await self._processor.process(
                ProcessorContext(
                    request=claimed.request,
                    mode=claimed.run.mode,
                    work_item_kind=claimed.work_item.kind,
                    graph_thread_id=claimed.run.graph_thread_id,
                )
            )
        except Exception as error:
            await self._store.fail(
                claimed.work_item.id,
                lease_token=lease_token,
                occurred_at=self._clock(),
                error=_safe_error(error),
                retry_delay=self._retry_delay(claimed.work_item.attempt_count),
            )
            return True

        await self._store.complete(
            claimed.work_item.id,
            lease_token=lease_token,
            occurred_at=self._clock(),
            result=outcome.result,
            metrics=outcome.metrics,
            model_name=outcome.model_name,
        )
        return True

    def _retry_delay(self, attempt_count: int) -> timedelta:
        multiplier = 2 ** max(attempt_count - 1, 0)
        calculated = timedelta(seconds=self._base_retry_delay.total_seconds() * multiplier)
        return calculated if calculated <= self._max_retry_delay else self._max_retry_delay


def _safe_error(error: Exception) -> WorkItemError:
    """只保存确定性分类，避免异常消息携带密钥或模型输入。"""

    error_code = type(error).__name__[:100] or "worker_error"
    message = "调查处理器调用超时" if isinstance(error, TimeoutError) else "调查处理器执行失败"
    return WorkItemError(error_code=error_code, message=message)
