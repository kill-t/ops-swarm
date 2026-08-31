"""单次或常驻领取并处理持久调查任务的 Worker。"""

import asyncio
import logging
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime, timedelta
from typing import Protocol, Self

from opentelemetry import trace
from opentelemetry.trace import SpanKind, Status, StatusCode, Tracer
from pydantic import model_validator

from opsswarm.investigation.attempts import ModelUsageTelemetry
from opsswarm.investigation.challenge import validate_hypothesis_challenge
from opsswarm.investigation.models import (
    EvidenceObservation,
    HypothesisChallenge,
    InvestigationArtifacts,
    InvestigationDataSourceMode,
    InvestigationMetrics,
    InvestigationMode,
    InvestigationRequest,
    InvestigationRunResult,
    SpecialistReport,
    StrictModel,
    WorkItemError,
    WorkItemKind,
)
from opsswarm.investigation.persistence import InvestigationStore
from opsswarm.observability import extract_trace_context, mark_span_error
from opsswarm.worker.usage import ModelUsageTracker

logger = logging.getLogger(__name__)


class ProcessorOutcome(StrictModel):
    """处理器交给持久化层的结构化成功结果。"""

    result: InvestigationRunResult
    evidence_items: tuple[EvidenceObservation, ...]
    specialist_reports: tuple[SpecialistReport, ...] = ()
    hypothesis_challenge: HypothesisChallenge
    metrics: InvestigationMetrics | None
    model_name: str

    @model_validator(mode="after")
    def validate_challenge(self) -> Self:
        """让伪造质疑结果在处理器边界失败并进入 Worker 重试。"""

        artifacts = InvestigationArtifacts(
            result=self.result,
            evidence_items=self.evidence_items,
            specialist_reports=self.specialist_reports,
        )
        validate_hypothesis_challenge(artifacts, self.hypothesis_challenge)
        return self


class ProcessorContext(StrictModel):
    """Worker 交给处理器的持久路由上下文。"""

    request: InvestigationRequest
    mode: InvestigationMode
    data_source_mode: InvestigationDataSourceMode = InvestigationDataSourceMode.FROZEN
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
        processing_timeout: timedelta = timedelta(seconds=100),
        base_retry_delay: timedelta = timedelta(seconds=5),
        max_retry_delay: timedelta = timedelta(minutes=5),
        data_source_mode: InvestigationDataSourceMode = InvestigationDataSourceMode.FROZEN,
        model_name: str | None = None,
        usage_tracker: ModelUsageTracker | None = None,
        tracer: Tracer | None = None,
    ) -> None:
        if base_retry_delay < timedelta(0):
            raise ValueError("base_retry_delay 不能小于 0")
        if max_retry_delay < base_retry_delay:
            raise ValueError("max_retry_delay 不能小于 base_retry_delay")
        if processing_timeout <= timedelta(0):
            raise ValueError("processing_timeout 必须大于 0")
        if processing_timeout >= lease_duration:
            raise ValueError("processing_timeout 必须小于 lease_duration")
        self._store = store
        self._processor = processor
        self._worker_id = worker_id
        self._clock = clock
        self._lease_duration = lease_duration
        self._processing_timeout = processing_timeout
        self._base_retry_delay = base_retry_delay
        self._max_retry_delay = max_retry_delay
        self._data_source_mode = data_source_mode
        self._model_name = model_name
        self._usage_tracker = usage_tracker
        self._tracer = tracer or trace.get_tracer(__name__)

    async def run_once(self) -> bool:
        """领取至多一个任务；无可用任务返回 False。"""

        claimed = await self._store.claim_next(
            worker_id=self._worker_id,
            occurred_at=self._clock(),
            lease_duration=self._lease_duration,
            data_source_mode=self._data_source_mode,
            model_name=self._model_name,
            model_pricing=(
                self._usage_tracker.pricing if self._usage_tracker is not None else None
            ),
        )
        if claimed is None:
            return False
        logger.info(
            "领取调查任务 work_item_id=%s run_id=%s mode=%s data_source_mode=%s attempt=%s",
            claimed.work_item.id,
            claimed.run.id,
            claimed.run.mode.value,
            claimed.run.data_source_mode.value,
            claimed.work_item.attempt_count,
        )
        lease_token = claimed.work_item.lease_token
        if lease_token is None:
            raise RuntimeError("已领取任务缺少 lease_token")
        if self._usage_tracker is not None:
            self._usage_tracker.begin_attempt()

        parent_context = extract_trace_context(claimed.work_item.trace_context)
        with self._tracer.start_as_current_span(
            "opsswarm.investigation.attempt",
            context=parent_context,
            kind=SpanKind.CONSUMER,
            attributes={
                "messaging.system": "postgresql",
                "messaging.operation.name": "process",
                "messaging.message.id": str(claimed.work_item.id),
                "opsswarm.investigation.run_id": str(claimed.run.id),
                "opsswarm.investigation.mode": claimed.run.mode.value,
                "opsswarm.investigation.data_source_mode": claimed.run.data_source_mode.value,
                "opsswarm.worker.attempt": claimed.work_item.attempt_count,
            },
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            try:
                outcome = await asyncio.wait_for(
                    self._processor.process(
                        ProcessorContext(
                            request=claimed.request,
                            mode=claimed.run.mode,
                            data_source_mode=claimed.run.data_source_mode,
                            work_item_kind=claimed.work_item.kind,
                            graph_thread_id=claimed.run.graph_thread_id,
                        )
                    ),
                    timeout=self._processing_timeout.total_seconds(),
                )
            except Exception as error:
                usage = self._finish_usage()
                mark_span_error(span, error)
                await self._store.fail(
                    claimed.work_item.id,
                    lease_token=lease_token,
                    occurred_at=self._clock(),
                    error=_safe_error(error),
                    retry_delay=self._retry_delay(claimed.work_item.attempt_count),
                    usage=usage,
                )
                logger.warning(
                    "调查任务进入重试 work_item_id=%s run_id=%s error_code=%s",
                    claimed.work_item.id,
                    claimed.run.id,
                    type(error).__name__[:100],
                )
                return True

            usage = self._finish_usage()
            await self._store.complete(
                claimed.work_item.id,
                lease_token=lease_token,
                occurred_at=self._clock(),
                result=outcome.result,
                evidence_items=outcome.evidence_items,
                specialist_reports=outcome.specialist_reports,
                hypothesis_challenge=outcome.hypothesis_challenge,
                metrics=outcome.metrics,
                model_name=outcome.model_name,
                usage=usage,
            )
            span.set_status(Status(StatusCode.OK))
            logger.info(
                "完成调查任务 work_item_id=%s run_id=%s",
                claimed.work_item.id,
                claimed.run.id,
            )
        return True

    async def run_forever(
        self,
        *,
        poll_interval: timedelta,
        stop_event: asyncio.Event | None = None,
    ) -> None:
        """持续领取任务；停止后不再领取新任务，当前任务先完成结算。"""

        if not timedelta(milliseconds=100) <= poll_interval <= timedelta(minutes=1):
            raise ValueError("poll_interval 必须在 100 毫秒到 1 分钟之间")
        while stop_event is None or not stop_event.is_set():
            processed = await self.run_once()
            if processed:
                continue
            if stop_event is None:
                await asyncio.sleep(poll_interval.total_seconds())
                continue
            with suppress(TimeoutError):
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=poll_interval.total_seconds(),
                )

    def _retry_delay(self, attempt_count: int) -> timedelta:
        multiplier = 2 ** max(attempt_count - 1, 0)
        calculated = timedelta(seconds=self._base_retry_delay.total_seconds() * multiplier)
        return calculated if calculated <= self._max_retry_delay else self._max_retry_delay

    def _finish_usage(self) -> ModelUsageTelemetry:
        if self._usage_tracker is None:
            return ModelUsageTelemetry()
        return self._usage_tracker.finish_attempt()


def _safe_error(error: Exception) -> WorkItemError:
    """只保存确定性分类，避免异常消息携带密钥或模型输入。"""

    error_code = type(error).__name__[:100] or "worker_error"
    message = "调查处理器调用超时" if isinstance(error, TimeoutError) else "调查处理器执行失败"
    return WorkItemError(error_code=error_code, message=message)
