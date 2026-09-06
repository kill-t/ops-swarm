"""查询等待结束不能提前释放真实线程资源。"""

import asyncio
from contextvars import ContextVar
from threading import get_ident

import pytest

from opsswarm.investigation.query_execution import (
    EvidenceQueryCapacityExceeded,
    EvidenceQueryExecutorClosed,
    EvidenceQueryTimeout,
    ReadOnlyQueryExecutor,
)
from tests.async_evidence_helpers import BlockedQuery, drain


@pytest.mark.asyncio
async def test_three_blocking_queries_overlap_and_keep_contexts_isolated():
    executor = ReadOnlyQueryExecutor()
    gates = [BlockedQuery() for _ in range(3)]
    identity = ContextVar("test-query-identity", default="outside")
    observed = []

    async def invoke(index):
        token = identity.set(f"task-{index}")
        try:

            def query():
                observed.append((identity.get(), get_ident()))
                gates[index]()
                return identity.get()

            return await executor.execute(query)
        finally:
            identity.reset(token)

    tasks = [asyncio.create_task(invoke(i)) for i in range(3)]
    try:
        await asyncio.wait_for(asyncio.gather(*(q.entered.wait() for q in gates)), 2)
        assert all(not task.done() for task in tasks)
        assert {item[0] for item in observed} == {"task-0", "task-1", "task-2"}
        assert len({item[1] for item in observed}) == 3
        assert get_ident() not in {item[1] for item in observed}
        assert identity.get() == "outside"
        await drain(executor, *gates)
        assert await asyncio.gather(*tasks) == ["task-0", "task-1", "task-2"]
    finally:
        await drain(executor, *gates)
        executor.close()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["timeout", "cancel"])
@pytest.mark.parametrize("late_error", [False, True])
async def test_abandoned_query_keeps_slot_until_real_completion(mode, late_error):
    executor = ReadOnlyQueryExecutor(max_queries=1, timeout_seconds=0.1)
    gate = BlockedQuery(result="late", error=RuntimeError("late") if late_error else None)
    loop = asyncio.get_running_loop()
    old_handler = loop.get_exception_handler()
    unhandled = []
    loop.set_exception_handler(lambda _, context: unhandled.append(context))
    task = asyncio.create_task(executor.execute(gate))
    try:
        await asyncio.wait_for(gate.entered.wait(), 2)
        if mode == "cancel":
            task.cancel()
        with pytest.raises(EvidenceQueryTimeout if mode == "timeout" else asyncio.CancelledError):
            await task
        never_started = BlockedQuery()
        for _ in range(3):
            with pytest.raises(EvidenceQueryCapacityExceeded):
                await executor.execute(never_started)
        assert never_started.calls == 0
        assert not gate.finished.is_set()
        await drain(executor, gate)
        assert await executor.execute(lambda: "fresh") == "fresh"
        assert unhandled == []
    finally:
        await drain(executor, gate)
        executor.close()
        await asyncio.gather(task, return_exceptions=True)
        loop.set_exception_handler(old_handler)


@pytest.mark.asyncio
async def test_source_timeout_is_preserved_and_closed_executor_rejects_new_queries():
    executor = ReadOnlyQueryExecutor(max_queries=1)
    error = TimeoutError("来源自己的客户端超时")

    def fail():
        raise error

    with pytest.raises(TimeoutError) as caught:
        await executor.execute(fail)
    assert type(caught.value) is TimeoutError and caught.value.args == error.args
    assert await executor.execute(lambda: 1) == 1
    executor.close()
    executor.close()
    with pytest.raises(EvidenceQueryExecutorClosed):
        await executor.execute(lambda: 2)


@pytest.mark.asyncio
async def test_close_does_not_block_event_loop_or_release_running_query():
    executor = ReadOnlyQueryExecutor(max_queries=1)
    gate = BlockedQuery()
    task = asyncio.create_task(executor.execute(gate))
    try:
        await asyncio.wait_for(gate.entered.wait(), 2)
        executor.close()
        assert not gate.finished.is_set() and executor._pending == 1
        with pytest.raises(EvidenceQueryExecutorClosed):
            await executor.execute(lambda: None)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        await drain(executor, gate)
        executor.close()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_queries": 0},
        {"max_queries": 4},
        {"max_queries": True},
        {"timeout_seconds": 0},
        {"timeout_seconds": 31},
        {"timeout_seconds": float("nan")},
    ],
)
def test_executor_rejects_unbounded_configuration(kwargs):
    with pytest.raises(ValueError):
        ReadOnlyQueryExecutor(**kwargs)
