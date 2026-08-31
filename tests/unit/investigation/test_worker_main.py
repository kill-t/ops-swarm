import asyncio
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest

from opsswarm.investigation.models import InvestigationDataSourceMode
from opsswarm.investigation.persistence import InvestigationStore
from opsswarm.worker.main import parse_arguments
from opsswarm.worker.runner import InvestigationProcessor, InvestigationWorker


class IdleStore:
    """首次轮询即请求停止，用于验证空闲等待可中断。"""

    def __init__(self, stop_event: asyncio.Event) -> None:
        self.stop_event = stop_event
        self.claim_count = 0

    async def claim_next(self, **_: object) -> None:
        self.claim_count += 1
        self.stop_event.set()
        return None


class UnusedProcessor:
    async def process(self, context: object) -> None:
        del context
        raise AssertionError("空闲 Worker 不应调用处理器")


def test_worker_command_defaults_to_daemon_and_keeps_once_diagnostic() -> None:
    assert parse_arguments([]) is False
    assert parse_arguments(["--once"]) is True


@pytest.mark.asyncio
async def test_idle_worker_stops_without_claiming_another_task() -> None:
    stop_event = asyncio.Event()
    store = IdleStore(stop_event)
    worker = InvestigationWorker(
        store=cast(InvestigationStore, store),
        processor=cast(InvestigationProcessor, UnusedProcessor()),
        worker_id="idle-worker",
        clock=lambda: datetime.now(UTC),
        lease_duration=timedelta(seconds=10),
        processing_timeout=timedelta(seconds=5),
        data_source_mode=InvestigationDataSourceMode.FROZEN,
    )

    await worker.run_forever(
        poll_interval=timedelta(milliseconds=100),
        stop_event=stop_event,
    )

    assert store.claim_count == 1


@pytest.mark.asyncio
async def test_worker_rejects_invalid_poll_and_processing_windows() -> None:
    stop_event = asyncio.Event()
    store = cast(InvestigationStore, IdleStore(stop_event))
    processor = cast(InvestigationProcessor, UnusedProcessor())

    with pytest.raises(ValueError, match="processing_timeout"):
        InvestigationWorker(
            store=store,
            processor=processor,
            worker_id="invalid-worker",
            clock=lambda: datetime.now(UTC),
            lease_duration=timedelta(seconds=5),
            processing_timeout=timedelta(seconds=5),
        )

    worker = InvestigationWorker(
        store=store,
        processor=processor,
        worker_id="invalid-poll-worker",
        clock=lambda: datetime.now(UTC),
        lease_duration=timedelta(seconds=10),
        processing_timeout=timedelta(seconds=5),
    )
    with pytest.raises(ValueError, match="poll_interval"):
        await worker.run_forever(poll_interval=timedelta(milliseconds=99))
