"""完整专业链上的独立综合、可信读取、恢复与原生拒绝。"""

import asyncio
import copy
import json
from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from alembic import command
from opsswarm.investigation.budget.model_contracts import ReportedModelUsage
from opsswarm.investigation.reinvestigation_synthesis.contracts import (
    SynthesisError,
    SynthesisModelResult,
)
from opsswarm.investigation.reinvestigation_synthesis.job_store import SynthesisJobStore
from opsswarm.investigation.reinvestigation_synthesis.ledger import SynthesisLedger
from opsswarm.investigation.reinvestigation_synthesis.queries import SynthesisQueries
from opsswarm.investigation.reinvestigation_synthesis.worker import SynthesisWorker
from opsswarm.investigation.synthesis.contracts import OpenHypothesisProposal
from tests.api import test_reinvestigation_execution_api as execution
from tests.api.test_reinvestigation_execution_api import (
    current_fixture_time as current_fixture_time,
)
from tests.api.test_reinvestigation_execution_api import (
    initial_reflow_capacity as initial_reflow_capacity,
)
from tests.api.test_reinvestigation_execution_api import wait_context as wait_context
from tests.identity_helpers import auth_header
from tests.integration.test_candidate_approval_migration import config

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


class Model:
    def __init__(self, hook=None):
        self.calls = []
        self.hook = hook
        self.usage = ReportedModelUsage(input_tokens=80, output_tokens=20, total_tokens=100)

    async def invoke(self, model_input):
        self.calls.append(model_input)
        body = json.loads(model_input.messages[1].content)
        if self.hook:
            await self.hook()
        ids = [e["evidence_id"] for p in body["professionals"] for e in p["evidence"]["evidence"]]
        return SynthesisModelResult(
            proposal=OpenHypothesisProposal(
                statement="新窗口异常仍待独立审查",
                confidence=0.2,
                supporting_evidence_ids=(ids[0],),
                counter_evidence_ids=tuple(ids[1:]),
                unresolved_questions=("仍需核验其他因果解释",),
            )
        )


async def setup(c, kind="terminal_handoff", roles=("change",)):
    await execution.prepare(c, kind, roles, wait_window=roles != ("change",))
    c.synthesis_store = SynthesisJobStore(c.db, clock=c.clock)
    c.synthesis_ledger = SynthesisLedger(c.db, clock=c.clock)
    c.app.state.reflow_synthesis_queries = SynthesisQueries(c.db, clock=c.clock)
    c.synthesis_base = f"/api/v1/investigations/{c.started.run.id}/reinvestigation-synthesis"
    c.synthesis_errors = []
    fail = c.synthesis_store.fail

    async def record_failure(*args, **kwargs):
        import sys

        c.synthesis_errors.append(str(sys.exception()))
        return await fail(*args, **kwargs)

    c.synthesis_store.fail = record_failure


async def professionals(c, roles):
    model = execution.FakeModel()
    evidence = execution.FakeEvidence()
    for _ in roles:
        assert await execution.worker(c, evidence, model).run_once()
    view = await execution.get(c)
    assert view.status_code == 200 and all(
        t["status"] == "succeeded" for t in view.json()["tasks"]
    ), view.text
    return view.json()


def worker(c, model):
    return SynthesisWorker(
        c.synthesis_store,
        c.synthesis_ledger,
        model_factory=lambda _: model,
        worker_id="synthesis-test-135",
    )


async def get(c, headers=None):
    return await c.client.get(
        c.synthesis_base, headers=auth_header("viewer") if headers is None else headers
    )


@pytest.mark.parametrize(
    "wait_context,kind",
    [("candidate_with_change", "terminal_handoff"), ("human_clean_with_change", "human_route")],
    indirect=["wait_context"],
)
async def test_complete_source_synthesis_and_native_integrity(wait_context, kind):
    c = wait_context
    roles = ("log", "metric", "change")
    await setup(c, kind, roles)
    assert (await get(c)).json()["status"] == "pending"
    assert not await c.synthesis_store.prepare_next()
    old = await execution.dispatch.coordination.root_facts(c)
    professional = await professionals(c, roles)
    before = await execution.dispatch.coordination.calls(c)
    assert (await get(c, {})).status_code == 401
    assert (await get(c, auth_header("alert_source"))).status_code == 403

    async def inflight():
        async with c.db() as s:
            call = await s.scalar(
                text(
                    "SELECT id FROM investigation_call_reservations "
                    "WHERE operation='synthesize_reinvestigation'"
                )
            )
        for suffix in ("model-input", "context-manifest", "interaction"):
            url = f"/api/v1/investigations/{c.started.run.id}/calls/{call}/{suffix}"
            assert (await c.client.get(url, headers=auth_header("viewer"))).status_code == 200
            assert (await c.client.get(url)).status_code == 401
        assert (await execution.get(c)).status_code == 200
        assert (await execution.dispatch.coordination.get(c)).status_code == 200

    model = Model(inflight)
    assert await worker(c, model).run_once()
    view = await get(c)
    assert view.status_code == 200 and view.json()["status"] == "succeeded", (
        view.text,
        c.synthesis_errors,
    )
    body = view.json()
    assert view.headers["cache-control"] == "no-store"
    assert body["result"]["review_status"] == "pending_review"
    assert len(model.calls) == 1
    source = json.loads(model.calls[0].messages[1].content)
    assert len(source["professionals"]) == 3 and len(source["materials"]) == 10
    after = await execution.dispatch.coordination.calls(c)
    assert after[:-1] == before and after[-1]["operation"] == "synthesize_reinvestigation"
    assert after[-1]["reflow_synthesis_attempt_id"] is not None
    assert old == await execution.dispatch.coordination.root_facts(c)
    assert professional == (await execution.get(c)).json()
    assert not await worker(c, model).run_once()
    async with c.db() as s:
        assert await s.scalar(
            text(
                "SELECT reflow_synthesis_packet(source)=packet "
                "FROM investigation_reflow_synthesis_jobs"
            )
        )
        snapshot = await s.scalar(
            text("SELECT reflow_synthesis_budget_snapshot(:id,clock_timestamp())"),
            {"id": c.started.run.id},
        )
    from opsswarm.investigation.budget.queries import CallBudgetQueries

    assert snapshot == (await CallBudgetQueries(c.db).get_snapshot(c.started.run.id)).model_dump(
        mode="json"
    )
    for sql in [
        "UPDATE investigation_reflow_synthesis_jobs SET source_sha256=repeat('0',64)",
        "DELETE FROM investigation_reflow_synthesis_results",
        "DELETE FROM investigation_reflow_synthesis_attempts",
        "UPDATE investigation_reflow_synthesis_results SET content='{}'",
    ]:
        with pytest.raises(DBAPIError):
            async with c.db() as s, s.begin():
                await s.execute(text(sql))
    async with c.db() as s:
        original = await s.scalar(text("SELECT source FROM investigation_reflow_synthesis_jobs"))
    for updates in (
        {"supporting_evidence_ids": ["foreign"]},
        {"counter_evidence_ids": body["result"]["proposal"]["supporting_evidence_ids"]},
        {"unknown_field": "not-allowed"},
    ):
        invalid = body["result"]["proposal"] | updates
        with pytest.raises(DBAPIError):
            async with c.db() as session, session.begin():
                await session.execute(
                    text(
                        "SELECT assert_reflow_synthesis_proposal("
                        "CAST(:proposal AS jsonb),CAST(:source AS jsonb))"
                    ),
                    {"proposal": json.dumps(invalid), "source": json.dumps(original)},
                )
    forged = copy.deepcopy(original)
    forged["professionals"][0]["result"]["report"]["summary"] = "伪造重算hash仍应拒绝"
    # 特权模拟材料损坏并回滚，SQL源重建必须独立于保存的综合正文。
    with pytest.raises(DBAPIError):
        async with c.db() as s, s.begin():
            await s.execute(text("SET LOCAL session_replication_role='replica'"))
            await s.execute(
                text(
                    "UPDATE investigation_reflow_synthesis_jobs "
                    "SET source=CAST(:body AS jsonb),"
                    "source_sha256=reflow_hash(CAST(:body AS jsonb))"
                ),
                {"body": json.dumps(forged)},
            )
            await s.execute(text("SET LOCAL session_replication_role='origin'"))
            await s.execute(
                text(
                    "SELECT assert_reflow_synthesis_source(j,false) "
                    "FROM investigation_reflow_synthesis_jobs j"
                )
            )
    with pytest.raises(DBAPIError):
        await asyncio.to_thread(command.downgrade, config(), "0089_reflow_execution")
    assert (await get(c)).json() == body


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_success_call_reuse_after_result_crash(wait_context):
    c = wait_context
    await setup(c)
    await professionals(c, ("change",))
    assert await c.synthesis_store.prepare_next()
    claim = await c.synthesis_store.claim_next(
        worker_id="expired-synthesis", lease_duration=timedelta(seconds=30)
    )
    assert claim is not None
    _, packet = await c.synthesis_store.get_packet(claim)
    grant = await c.synthesis_ledger.reserve(claim)
    late_model = Model()
    late = await late_model.invoke(packet.model_input)
    # 真实时钟越期发生在UPDATE之后、COMMIT之前，必须由延迟守卫整笔回滚。
    with pytest.raises(DBAPIError):
        async with c.db() as session, session.begin():
            await session.execute(
                text(
                    "UPDATE investigation_call_reservations SET status='succeeded',"
                    "finished_at=clock_timestamp(),call_result=CAST(:result AS jsonb),"
                    "model_usage=CAST(:usage AS jsonb) WHERE id=:id"
                ),
                {
                    "id": grant.record.id,
                    "result": late.model_dump_json(),
                    "usage": late_model.usage.model_dump_json(),
                },
            )
            while await session.scalar(
                text("SELECT clock_timestamp()<:deadline"), {"deadline": claim.lease_expires_at}
            ):
                await asyncio.sleep(1)
    with pytest.raises(SynthesisError, match="租约"):
        await c.synthesis_ledger.settle(
            claim, grant.record.id, status="succeeded", result=late, usage=late_model.usage
        )
    original = c.synthesis_store.complete

    async def crash(*args):
        raise RuntimeError("模拟综合结果落库前崩溃")

    c.synthesis_store.complete = crash
    model = Model()
    assert await worker(c, model).run_once()
    c.synthesis_store.complete = original
    await asyncio.sleep(2)
    assert await worker(c, model).run_once()
    view = await get(c)
    assert view.json()["status"] == "succeeded", (view.text, c.synthesis_errors)
    assert len(model.calls) == 1 and len(view.json()["attempts"]) == 3
    calls = await execution.dispatch.coordination.calls(c)
    new = [call for call in calls if call["operation"] == "synthesize_reinvestigation"]
    assert len(new) == 2 and new[0]["status"] == "unknown" and new[1]["status"] == "succeeded"
    assert new[0]["model_allocation"] == new[1]["model_allocation"]


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_new_alert_retains_usage_without_hypothesis(wait_context):
    c = wait_context
    await setup(c)
    await professionals(c, ("change",))

    async def alert():
        await execution.dispatch.coordination.admission.attach_old_alert(c)

    model = Model(alert)
    assert await worker(c, model).run_once()
    view = await get(c)
    assert view.status_code == 200 and view.json()["status"] == "failed", (
        view.text,
        c.synthesis_errors,
    )
    assert view.json()["result"] is None
    calls = await execution.dispatch.coordination.calls(c)
    assert calls[-1]["status"] == "unknown" and calls[-1]["model_usage"] == model.usage.model_dump(
        mode="json"
    )


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_failed_professional_blocks_without_model(wait_context):
    c = wait_context
    await setup(c)
    assert await c.execution_store.prepare_next()
    claim = await c.execution_store.claim_next(worker_id="test-failed")
    assert claim is not None
    await c.execution_store.fail(claim, error_code="execution_failed", retryable=False)
    model = Model()
    assert await worker(c, model).run_once()
    body = (await get(c)).json()
    assert body["status"] == "blocked" and body["error_code"] == "synthesis_professionals_failed"
    assert not model.calls and body["packet"] is None
