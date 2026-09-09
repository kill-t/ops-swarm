"""真实事务阻断伪造历史、越权来源、迟到写入与并发重复调用。"""

import asyncio
import json
from datetime import timedelta
from uuid import uuid4

import pytest

from opsswarm.investigation.budget.contracts import CallOperation
from opsswarm.investigation.budget.queries import CallBudgetQueries
from opsswarm.investigation.loop.contracts import (
    AssistantMessage,
    EvidenceToolRequest,
    EvidenceToolResult,
    LoopModelResult,
    ReadEvidenceArguments,
    ToolCall,
    ToolMessage,
)
from opsswarm.investigation.models import WorkItemError
from opsswarm.investigation.tasks.contracts import TaskOutcome
from opsswarm.investigation.tasks.errors import TaskFactConflict
from tests.loop_helpers import loop_policy, prepared_loop, reserve_loop

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def read_result(packet, *, tool_id="read-1"):
    catalog = json.loads(packet.model_input.messages[1].content)["evidence_catalog"]
    arguments = ReadEvidenceArguments(
        evidence_id=catalog["entries"][0]["evidence_id"], snapshot_sha256=catalog["snapshot_sha256"]
    )
    return LoopModelResult(
        messages=(
            AssistantMessage(
                content="",
                tool_calls=(
                    ToolCall(
                        id=tool_id,
                        name="read_evidence",
                        args=arguments.model_dump(mode="json"),
                    ),
                ),
            ),
        )
    ), arguments


async def committed_read_request(db):
    prepared, packet, store, service = await prepared_loop(db)
    grant = await reserve_loop(prepared, packet, store)
    response, arguments = read_result(packet)
    await store.settle(
        prepared[1].run.id,
        grant.record.id,
        lease_token=prepared[1].work_item.lease_token,
        status="succeeded",
        call_result=response,
    )
    request = EvidenceToolRequest(
        source_call_id=grant.record.id, tool_call_id="read-1", arguments=arguments
    )
    return prepared, packet, store, service, grant, request


async def reserve_read(prepared, store, request, index=0):
    _, claim, _, _, task, attempt, _ = prepared
    return await store.reserve(
        claim.run.id,
        lease_token=claim.work_item.lease_token,
        task_id=task.id,
        task_attempt_id=attempt.id,
        operation=CallOperation.READ_LOG_EVIDENCE,
        invocation_index=index,
        tool_request=request,
    )


@pytest.mark.parametrize(
    "mutation", ["prompt", "scope", "schema", "tool_schema", "history", "manifest"]
)
async def test_atomic_model_reservation_rejects_forged_context(database_session_factory, mutation):
    prepared, packet, store, _ = await prepared_loop(database_session_factory)
    inputs = packet.model_input
    if mutation == "prompt":
        inputs = inputs.model_copy(
            update={
                "messages": (
                    inputs.messages[0].model_copy(update={"content": "改变规则"}),
                    inputs.messages[1],
                )
            }
        )
    elif mutation == "scope":
        packet = packet.model_copy(
            update={"manifest": packet.manifest.model_copy(update={"task_id": uuid4()})}
        )
    elif mutation == "schema":
        inputs = inputs.model_copy(update={"output_schema": {"type": "string"}})
    elif mutation == "tool_schema":
        inputs = inputs.model_copy(update={"tool_schema": {"tools": []}})
    elif mutation == "history":
        model_result, _ = read_result(packet)
        inputs = inputs.model_copy(update={"messages": (*inputs.messages, *model_result.messages)})
    else:
        packet = packet.model_copy(
            update={
                "manifest": packet.manifest.model_copy(
                    update={"materials": packet.manifest.materials[:-1]}
                )
            }
        )
    with pytest.raises(TaskFactConflict):
        await reserve_loop(prepared, packet.model_copy(update={"model_input": inputs}), store)
    snapshot = await CallBudgetQueries(database_session_factory).get_snapshot(prepared[1].run.id)
    assert snapshot.reserved_model_calls == 0


@pytest.mark.parametrize("mutation", ["other_run", "tool_id", "evidence", "hash", "slot"])
async def test_tool_request_must_match_committed_model_and_scope(
    database_session_factory, mutation
):
    prepared, _, store, _, grant, request = await committed_read_request(database_session_factory)
    if mutation == "other_run":
        other = await committed_read_request(database_session_factory)
        request = request.model_copy(update={"source_call_id": other[4].record.id})
    elif mutation == "tool_id":
        request = request.model_copy(update={"tool_call_id": "forged"})
    elif mutation in ("evidence", "hash"):
        args = request.arguments.model_copy(
            update={"evidence_id": "foreign-evidence"}
            if mutation == "evidence"
            else {"snapshot_sha256": "0" * 64}
        )
        request = request.model_copy(update={"arguments": args})
    with pytest.raises(TaskFactConflict):
        await reserve_read(prepared, store, request, 1 if mutation == "slot" else 0)
    snapshot = await CallBudgetQueries(database_session_factory).get_snapshot(prepared[1].run.id)
    assert snapshot.reserved_tool_calls == 1 and snapshot.reserved_model_calls == 1
    assert next(c for c in snapshot.calls if c.id == grant.record.id).status == "succeeded"


async def test_concurrent_replay_and_unpaired_history_cannot_advance(database_session_factory):
    prepared, packet, store, service = await prepared_loop(database_session_factory)
    grants = await asyncio.gather(*(reserve_loop(prepared, packet, store) for _ in range(2)))
    assert sum(g.created for g in grants) == 1 and grants[0].record.id == grants[1].record.id
    response, arguments = read_result(packet)
    _, claim, _, _, task, attempt, _ = prepared
    with pytest.raises(TaskFactConflict):
        await service.prepare(task.id, attempt.id, CallOperation.ANALYZE_LOG, 1, loop_policy())
    await store.settle(
        claim.run.id,
        grants[0].record.id,
        lease_token=claim.work_item.lease_token,
        status="succeeded",
        call_result=response,
    )
    with pytest.raises(TaskFactConflict):
        await service.prepare(task.id, attempt.id, CallOperation.ANALYZE_LOG, 1, loop_policy())
    request = EvidenceToolRequest(
        source_call_id=grants[0].record.id, tool_call_id="read-1", arguments=arguments
    )
    reads = await asyncio.gather(*(reserve_read(prepared, store, request) for _ in range(2)))
    assert sum(g.created for g in reads) == 1
    with pytest.raises(TaskFactConflict):
        await service.prepare(task.id, attempt.id, CallOperation.ANALYZE_LOG, 1, loop_policy())
    content = await service.read(grants[0].record.id, task.id, arguments)
    result = EvidenceToolResult(
        message=ToolMessage(content=content, name="read_evidence", tool_call_id="read-1")
    )
    await store.settle(
        claim.run.id,
        reads[0].record.id,
        lease_token=claim.work_item.lease_token,
        status="succeeded",
        call_result=result,
    )
    packet = await service.prepare(task.id, attempt.id, CallOperation.ANALYZE_LOG, 1, loop_policy())
    assert len(packet.model_input.messages) == 4
    next_call = await reserve_loop(prepared, packet, store, 1)
    assert next_call.record.id != grants[0].record.id


@pytest.mark.parametrize("mutation", ["evidence", "pair", "oversized", "missing"])
async def test_tool_settlement_cannot_claim_a_forged_result(database_session_factory, mutation):
    prepared, _, store, service, grant, request = await committed_read_request(
        database_session_factory
    )
    read = await reserve_read(prepared, store, request)
    _, claim, _, _, task, _, _ = prepared
    content = await service.read(grant.record.id, task.id, request.arguments)
    if mutation == "evidence":
        data = json.loads(content)
        data["evidence"]["summary"] = "伪造的现场结论"
        content = json.dumps(data)
    if mutation == "oversized":
        content = "中" * 20000
    result = (
        None
        if mutation == "missing"
        else EvidenceToolResult(
            message=ToolMessage(
                content=content,
                name="read_evidence",
                tool_call_id="wrong" if mutation == "pair" else "read-1",
            )
        )
    )
    with pytest.raises((TaskFactConflict, ValueError)):
        await store.settle(
            claim.run.id,
            read.record.id,
            lease_token=claim.work_item.lease_token,
            status="succeeded",
            call_result=result,
        )
    row = next(
        c
        for c in (
            await CallBudgetQueries(database_session_factory).get_snapshot(claim.run.id)
        ).calls
        if c.id == read.record.id
    )
    assert row.status == "reserved"


@pytest.mark.parametrize("boundary", ["lease", "parent_failed"])
async def test_late_results_cannot_continue_a_stale_attempt(database_session_factory, boundary):
    prepared, _, store, _, grant, request = await committed_read_request(database_session_factory)
    parent, claim, clock, _, _, _, _ = prepared
    if boundary == "lease":
        clock.current += timedelta(seconds=31)
    else:
        await parent.fail(
            claim.work_item.id,
            lease_token=claim.work_item.lease_token,
            occurred_at=clock(),
            retry_delay=timedelta(0),
            error=WorkItemError(error_code="crash", message="模拟父尝试失效"),
        )
    with pytest.raises(TaskFactConflict):
        await reserve_read(prepared, store, request)
    original = (
        await CallBudgetQueries(database_session_factory).get_interaction(
            claim.run.id, grant.record.id
        )
    ).result
    with pytest.raises(TaskFactConflict):
        await store.settle(
            claim.run.id,
            grant.record.id,
            lease_token=claim.work_item.lease_token,
            status="succeeded",
            call_result=original,
        )


async def test_exact_settlement_replay_survives_task_end_but_new_results_do_not(
    database_session_factory,
):
    prepared, _, store, _, grant, request = await committed_read_request(database_session_factory)
    _, claim, _, tasks, task, attempt, _ = prepared
    unfinished = await reserve_read(prepared, store, request)
    queries = CallBudgetQueries(database_session_factory)
    result = (await queries.get_interaction(claim.run.id, grant.record.id)).result
    await tasks.finish_attempt(
        claim.run.id,
        task.id,
        attempt.id,
        lease_token=claim.work_item.lease_token,
        outcome=TaskOutcome(status="failed", error_code="interrupted", retryable=True),
    )
    replay = await store.settle(
        claim.run.id,
        grant.record.id,
        lease_token=claim.work_item.lease_token,
        status="succeeded",
        call_result=result,
    )
    assert replay.id == grant.record.id and replay.status == "succeeded"
    with pytest.raises(TaskFactConflict):
        await store.settle(
            claim.run.id,
            unfinished.record.id,
            lease_token=claim.work_item.lease_token,
            status="succeeded",
        )
