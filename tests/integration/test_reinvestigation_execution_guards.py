"""真实并发预留、原生归属及租约到期提交/接管的独立验收。"""

import asyncio
import copy
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from alembic import command
from opsswarm.approval.conditions.queries import raw_content_hash
from opsswarm.investigation.budget.queries import read_budget_snapshot
from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.investigation.reinvestigation_coordination.contracts import ReflowError
from opsswarm.investigation.reinvestigation_execution.contracts import ANALYZE, COLLECT
from tests.api import test_reinvestigation_execution_api as api
from tests.api.test_reinvestigation_execution_api import (
    current_fixture_time as current_fixture_time,
)
from tests.api.test_reinvestigation_execution_api import (
    initial_reflow_capacity as initial_reflow_capacity,
)
from tests.api.test_reinvestigation_execution_api import wait_context as wait_context
from tests.integration.test_candidate_approval_migration import config, encoded
from tests.integration.test_reinvestigation_coordination_migration import insert

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_0089_concurrent_budget_native_binding_and_actual_lease_takeover(wait_context):
    c = wait_context
    await api.prepare(c, roles=("log", "metric", "change"))
    before = await api.dispatch.coordination.root_facts(c)
    original_calls = await api.dispatch.coordination.calls(c)
    for _ in range(3):
        assert await c.execution_store.prepare_next()
    claims = [
        await c.execution_store.claim_next(
            worker_id=f"native-134-{i}", lease_duration=timedelta(seconds=300)
        )
        for i in range(3)
    ]
    assert all(claims) and len({cl.task_id for cl in claims}) == 3
    grants = await asyncio.gather(*(c.execution_ledger.reserve(cl, COLLECT) for cl in claims))
    async with c.db() as session:
        run = await session.get(InvestigationRunRow, c.started.run.id)
        native = await session.scalar(
            text("SELECT reflow_execution_budget_snapshot(:id,clock_timestamp())"), {"id": run.id}
        )
        assert native == (await read_budget_snapshot(session, run)).model_dump(mode="json")
    calls = await api.dispatch.coordination.calls(c)
    assert len(calls) == len(original_calls) + 3
    assert all(call["status"] == "reserved" for call in calls[-3:])
    for field, value in (
        ("reflow_task_attempt_id", str(uuid4())),
        ("reflow_task_id", str(claims[1].task_id)),
        ("task_id", str(uuid4())),
        ("kind", "model"),
    ):
        forged = {**calls[-3], "id": str(uuid4()), "sequence": len(calls) + 1, field: value}
        with pytest.raises(DBAPIError):
            async with c.db() as session, session.begin():
                await insert(session, "investigation_call_reservations", forged)
    # 只有成功标记没有同事务新快照，不得作为已完成取证保存。
    with pytest.raises(DBAPIError, match="快照"):
        async with c.db() as session, session.begin():
            await session.execute(
                text(
                    "UPDATE investigation_call_reservations SET status='succeeded',"
                    "finished_at=clock_timestamp() WHERE id=:id"
                ),
                {"id": grants[0].record.id},
            )
    evidence = api.FakeEvidence()
    packets = []
    for claim, grant in zip(claims, grants, strict=True):
        source, window = await c.execution_store.context(claim)
        snapshot = await evidence.collect(source.assignment, claim.task_id, window)
        assert await c.execution_ledger.save_evidence(claim, grant.record.id, snapshot)
        packet = await c.execution_ledger.prepare_packet(claim)
        packets.append(packet)
        async with c.db() as session:
            native = await session.scalar(
                text("SELECT reflow_execution_packet(:id)"), {"id": claim.task_id}
            )
            assert native == packet.model_dump(mode="json")
    async with c.db() as session:
        row = await session.scalar(
            text("SELECT snapshot FROM investigation_reflow_task_evidence WHERE task_id=:id"),
            {"id": claims[0].task_id},
        )
    damaged = copy.deepcopy(row)
    damaged["evidence"][0]["source"] = "frozen-history"
    with pytest.raises(DBAPIError):
        async with c.db() as session, session.begin():
            await session.execute(text("SET LOCAL session_replication_role='replica'"))
            await session.execute(
                text(
                    "UPDATE investigation_reflow_task_evidence "
                    "SET snapshot=CAST(:snapshot AS jsonb),"
                    "snapshot_sha256=:hash WHERE task_id=:id"
                ),
                {
                    "id": claims[0].task_id,
                    "snapshot": encoded(damaged),
                    "hash": raw_content_hash(damaged),
                },
            )
            await session.execute(text("SET LOCAL session_replication_role='origin'"))
            await session.execute(
                text("SELECT assert_reflow_execution_evidence(:id)"), {"id": claims[0].task_id}
            )
    models = await asyncio.gather(*(c.execution_ledger.reserve(cl, ANALYZE) for cl in claims))
    fake_model = api.FakeModel()
    results = [await fake_model.invoke(packet.model_input) for packet in packets]
    bad = results[0].report.model_copy(update={"evidence_ids": ("foreign-evidence",)})
    with pytest.raises(DBAPIError):
        async with c.db() as session, session.begin():
            await session.execute(
                text("SELECT assert_reflow_execution_report(CAST(:report AS jsonb),:id)"),
                {"id": claims[0].task_id, "report": encoded(bad.model_dump(mode="json"))},
            )
    for claim, grant, result in zip(claims[:2], models[:2], results[:2], strict=True):
        assert await c.execution_ledger.settle(
            claim, grant.record.id, status="succeeded", result=result
        )
        await c.execution_store.complete(claim, grant.record.id)
    # 旧尝试失联额度不退，下一尝试复用成功取证及冻结输入。
    await c.execution_store.fail(claims[2], error_code="simulated_loss", retryable=True)
    await asyncio.sleep(2)
    late = await c.execution_store.claim_next(
        worker_id="native-134-late", lease_duration=timedelta(seconds=30)
    )
    assert late is not None and late.number == 2
    grant = await c.execution_ledger.reserve(late, ANALYZE)
    with pytest.raises(DBAPIError, match="提交前"):
        async with c.db() as session, session.begin():
            await session.execute(
                text(
                    "UPDATE investigation_call_reservations SET status='succeeded',"
                    "finished_at=clock_timestamp(),call_result=CAST(:result AS jsonb) WHERE id=:id"
                ),
                {"id": grant.record.id, "result": encoded(results[2].model_dump(mode="json"))},
            )
            while await session.scalar(text("SELECT clock_timestamp()")) < late.lease_expires_at:
                await asyncio.sleep(0.5)
    with pytest.raises(ReflowError, match="租约"):
        await c.execution_ledger.settle(late, grant.record.id, status="unknown")
    with pytest.raises(ReflowError, match="租约"):
        await c.execution_store.complete(late, grant.record.id)
    await asyncio.sleep(0.2)
    recovered = await c.execution_store.claim_next(
        worker_id="native-134-recovered", lease_duration=timedelta(seconds=300)
    )
    assert recovered is not None and recovered.task_id == late.task_id and recovered.number == 3
    saved = await api.dispatch.coordination.calls(c)
    assert next(call for call in saved if call["id"] == str(grant.record.id))["status"] == "unknown"
    final = await c.execution_ledger.reserve(recovered, ANALYZE)
    assert final.record.sequence == grant.record.sequence + 1
    assert await c.execution_ledger.settle(
        recovered, final.record.id, status="succeeded", result=results[2]
    )
    await c.execution_store.complete(recovered, final.record.id)
    response = await api.get(c)
    assert response.status_code == 200, response.text
    assert {task["status"] for task in response.json()["tasks"]} == {"succeeded"}
    assert await api.dispatch.coordination.root_facts(c) == before
    assert len(evidence.calls) == 3
    for table in (
        "investigation_reflow_task_work",
        "investigation_reflow_task_attempts",
        "investigation_reflow_task_evidence",
        "investigation_reflow_task_packets",
        "investigation_reflow_task_results",
    ):
        with pytest.raises(DBAPIError):
            async with c.db() as session, session.begin():
                await session.execute(text(f"DELETE FROM {table}"))
    with pytest.raises(DBAPIError):
        await asyncio.to_thread(command.downgrade, config(), "0088_reflow_dispatch")
