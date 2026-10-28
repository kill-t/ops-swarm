"""真实完整链上的独立审查、同源绑定、租约恢复与可信读取。"""

import asyncio
import copy
import json
from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from alembic import command
from opsswarm.approval.conditions.queries import raw_content_hash
from opsswarm.investigation.budget.model_contracts import ReportedModelUsage
from opsswarm.investigation.budget.queries import CallBudgetQueries
from opsswarm.investigation.reinvestigation_review.contracts import (
    ReflowReviewError,
    ReflowReviewModelResult,
    ReflowReviewSource,
)
from opsswarm.investigation.reinvestigation_review.job_store import ReflowReviewJobStore
from opsswarm.investigation.reinvestigation_review.ledger import ReflowReviewLedger
from opsswarm.investigation.reinvestigation_review.queries import ReflowReviewQueries
from opsswarm.investigation.reinvestigation_review.worker import ReflowReviewWorker
from tests.api import test_reinvestigation_synthesis_api as synthesis
from tests.api.test_reinvestigation_synthesis_api import (
    current_fixture_time as current_fixture_time,
)
from tests.api.test_reinvestigation_synthesis_api import (
    initial_reflow_capacity as initial_reflow_capacity,
)
from tests.api.test_reinvestigation_synthesis_api import wait_context as wait_context
from tests.identity_helpers import auth_header
from tests.integration.test_candidate_approval_migration import config
from tests.unit.investigation.test_reinvestigation_review import proposal

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


class Model:
    def __init__(self, hook=None, direction="evaluate_for_routing"):
        self.calls = []
        self.hook = hook
        self.direction = direction
        self.usage = ReportedModelUsage(input_tokens=80, output_tokens=20, total_tokens=100)

    async def invoke(self, model_input):
        self.calls.append(model_input)
        source = ReflowReviewSource.model_validate_json(model_input.messages[1].content)
        if self.hook:
            await self.hook()
        return ReflowReviewModelResult(proposal=proposal(source, self.direction))


async def setup(c, kind="terminal_handoff", roles=("change",), reuse_synthesis=False):
    await synthesis.setup(c, kind, roles)
    c.review_store = ReflowReviewJobStore(c.db, clock=c.clock)
    c.review_ledger = ReflowReviewLedger(c.db, clock=c.clock)
    c.app.state.reflow_review_queries = ReflowReviewQueries(c.db, clock=c.clock)
    c.review_base = f"/api/v1/investigations/{c.started.run.id}/reinvestigation-review"
    assert not await c.review_store.prepare_next()
    await synthesis.professionals(c, roles)
    m = synthesis.Model()
    original = c.synthesis_store.complete
    if reuse_synthesis:

        async def crash(*args):
            raise RuntimeError("模拟135成功后结果提交前崩溃")

        c.synthesis_store.complete = crash
    assert await synthesis.worker(c, m).run_once()
    if reuse_synthesis:
        c.synthesis_store.complete = original
        await asyncio.sleep(2)
        assert await synthesis.worker(c, m).run_once()
        assert len(m.calls) == 1
    response = await synthesis.get(c)
    assert response.status_code == 200 and response.json()["status"] == "succeeded", response.text
    c.review_errors = []
    fail = c.review_store.fail

    async def record_failure(*args, **kwargs):
        import sys

        c.review_errors.append(str(sys.exception()))
        return await fail(*args, **kwargs)

    c.review_store.fail = record_failure


def worker(c, model):
    return ReflowReviewWorker(
        c.review_store, c.review_ledger, model_factory=lambda _: model, worker_id="review-test-136"
    )


async def get(c, headers=None):
    return await c.client.get(
        c.review_base, headers=auth_header("viewer") if headers is None else headers
    )


@pytest.mark.parametrize(
    "wait_context,kind",
    [("candidate_with_change", "terminal_handoff"), ("human_clean_with_change", "human_route")],
    indirect=["wait_context"],
)
async def test_two_sources_exact_review_and_native_protection(wait_context, kind):
    c = wait_context
    await setup(c, kind, ("log", "metric", "change"), reuse_synthesis=kind == "human_route")
    previous = (await synthesis.get(c)).json()
    before = await synthesis.execution.dispatch.coordination.calls(c)
    root = await synthesis.execution.dispatch.coordination.root_facts(c)
    assert (await get(c)).json()["status"] == "pending"
    assert (await get(c, {})).status_code == 401
    assert (await get(c, auth_header("alert_source"))).status_code == 403
    prepared = await asyncio.gather(c.review_store.prepare_next(), c.review_store.prepare_next())
    assert sorted(prepared) == [False, True]

    async def inflight():
        async with c.db() as session:
            call = await session.scalar(
                text(
                    "SELECT id FROM investigation_call_reservations "
                    "WHERE operation='review_reinvestigation'"
                )
            )
        for suffix in ("model-input", "context-manifest", "interaction"):
            path = f"/api/v1/investigations/{c.started.run.id}/calls/{call}/{suffix}"
            response = await c.client.get(path, headers=auth_header("viewer"))
            assert response.status_code == 200, response.text
            assert (await c.client.get(path)).status_code == 401
        assert (await synthesis.get(c)).status_code == 200
        assert (await synthesis.execution.get(c)).status_code == 200

    direction = "follow_up" if kind == "human_route" else "evaluate_for_routing"
    model = Model(inflight, direction)
    assert await worker(c, model).run_once()
    response = await get(c)
    assert response.status_code == 200 and response.json()["status"] == "succeeded", (
        response.text,
        c.review_errors,
    )
    body = response.json()
    assert (
        body["result"]["status"] == "recorded"
        and body["result"]["proposal"]["suggested_direction"] == direction
    )
    assert response.headers["cache-control"] == "no-store"
    assert previous == (await synthesis.get(c)).json()
    assert root == await synthesis.execution.dispatch.coordination.root_facts(c)
    assert len(model.calls) == 1
    source = json.loads(model.calls[0].messages[1].content)
    assert source["synthesis_result"] == previous["result"]
    assert len(source["materials"]) == 10 and len(source["professionals"]) == 3
    assert body["result"]["synthesis_result_sha256"] == source["synthesis_result_sha256"]
    calls = await synthesis.execution.dispatch.coordination.calls(c)
    assert calls[:-1] == before and calls[-1]["reflow_review_attempt_id"] is not None
    assert calls[-1]["reflow_synthesis_attempt_id"] is None
    assert not await worker(c, model).run_once()
    async with c.db() as session:
        assert await session.scalar(
            text("SELECT reflow_review_packet(source)=packet FROM investigation_reflow_review_jobs")
        )
        snapshot = await session.scalar(
            text("SELECT reflow_review_budget_snapshot(:id,clock_timestamp())"),
            {"id": c.started.run.id},
        )
    assert snapshot == (await CallBudgetQueries(c.db).get_snapshot(c.started.run.id)).model_dump(
        mode="json"
    )
    # 同轮新证据也不能冒充综合提案原先没有引用的支持证据。
    other = next(
        e["evidence_id"]
        for p in source["professionals"]
        for e in p["evidence"]["evidence"]
        if e["evidence_id"] not in source["synthesis_result"]["proposal"]["supporting_evidence_ids"]
    )
    for update in (
        {"accepted_supporting_evidence_ids": [other]},
        {"counter_evidence_ids": ["historical-only"]},
        {"support_is_sufficient": not body["result"]["proposal"]["support_is_sufficient"]},
        {"unknown_field": True},
        {
            "accepted_supporting_evidence_ids": source["synthesis_result"]["proposal"][
                "supporting_evidence_ids"
            ],
            "counter_evidence_ids": source["synthesis_result"]["proposal"][
                "supporting_evidence_ids"
            ],
        },
    ):
        invalid = body["result"]["proposal"] | update
        with pytest.raises(DBAPIError):
            async with c.db() as session, session.begin():
                await session.execute(
                    text(
                        "SELECT assert_reflow_review_proposal(CAST(:p AS jsonb),CAST(:s AS jsonb))"
                    ),
                    {"p": json.dumps(invalid), "s": json.dumps(source)},
                )
    for sql in [
        "UPDATE investigation_reflow_review_jobs SET source_sha256=repeat('0',64)",
        "DELETE FROM investigation_reflow_review_results",
        "DELETE FROM investigation_reflow_review_attempts",
        "UPDATE investigation_reflow_review_results SET content='{}'",
    ]:
        with pytest.raises(DBAPIError):
            async with c.db() as session, session.begin():
                await session.execute(text(sql))
    forged = copy.deepcopy(source)
    forged["synthesis_result"]["proposal"]["statement"] = "伪造提案并重算摘要"
    forged["synthesis_result_sha256"] = raw_content_hash(forged["synthesis_result"])
    with pytest.raises(DBAPIError):
        async with c.db() as session, session.begin():
            await session.execute(text("SET LOCAL session_replication_role='replica'"))
            await session.execute(
                text(
                    "UPDATE investigation_reflow_review_jobs SET source=CAST(:s AS jsonb),"
                    "source_sha256=reflow_hash(CAST(:s AS jsonb)),"
                    "packet=reflow_review_packet(CAST(:s AS jsonb)),"
                    "packet_sha256=reflow_hash(reflow_review_packet(CAST(:s AS jsonb)))"
                ),
                {"s": json.dumps(forged)},
            )
            await session.execute(text("SET LOCAL session_replication_role='origin'"))
            await session.execute(
                text(
                    "SELECT assert_reflow_review_source(j,false) "
                    "FROM investigation_reflow_review_jobs j"
                )
            )
    with pytest.raises(DBAPIError):
        await asyncio.to_thread(command.downgrade, config(), "0090_reflow_synthesis")
    assert (await get(c)).json() == body


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_expired_commit_takeover_and_success_reuse(wait_context):
    c = wait_context
    await setup(c)
    assert await c.review_store.prepare_next()
    claim = await c.review_store.claim_next(
        worker_id="expired-review", lease_duration=timedelta(seconds=30)
    )
    assert claim is not None
    _, packet = await c.review_store.get_packet(claim)
    grant = await c.review_ledger.reserve(claim)
    late_model = Model()
    late = await late_model.invoke(packet.model_input)
    with pytest.raises(DBAPIError):
        async with c.db() as session, session.begin():
            await session.execute(
                text(
                    "UPDATE investigation_call_reservations SET status='succeeded',"
                    "finished_at=clock_timestamp(),call_result=CAST(:r AS jsonb),"
                    "model_usage=CAST(:u AS jsonb) WHERE id=:id"
                ),
                {
                    "id": grant.record.id,
                    "r": late.model_dump_json(),
                    "u": late_model.usage.model_dump_json(),
                },
            )
            while await session.scalar(
                text("SELECT clock_timestamp()<:deadline"), {"deadline": claim.lease_expires_at}
            ):
                await asyncio.sleep(1)
    with pytest.raises(ReflowReviewError, match="租约"):
        await c.review_ledger.settle(
            claim, grant.record.id, status="succeeded", result=late, usage=late_model.usage
        )
    original = c.review_store.complete

    async def crash(*args):
        raise RuntimeError("模拟审查成功后结果写入中断")

    c.review_store.complete = crash
    model = Model(direction="human_review")
    assert await worker(c, model).run_once()
    c.review_store.complete = original
    await asyncio.sleep(2)
    assert await worker(c, model).run_once()
    response = await get(c)
    assert response.status_code == 200 and response.json()["status"] == "succeeded", (
        response.text,
        c.review_errors,
    )
    assert len(model.calls) == 1 and len(response.json()["attempts"]) == 3
    calls = [
        v
        for v in await synthesis.execution.dispatch.coordination.calls(c)
        if v["operation"] == "review_reinvestigation"
    ]
    assert len(calls) == 2 and calls[0]["status"] == "unknown" and calls[1]["status"] == "succeeded"
    assert calls[0]["model_allocation"] == calls[1]["model_allocation"]


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_new_alert_keeps_usage_but_rejects_review(wait_context):
    c = wait_context
    await setup(c)

    async def alert():
        await synthesis.execution.dispatch.coordination.admission.attach_old_alert(c)

    model = Model(alert)
    assert await worker(c, model).run_once()
    response = await get(c)
    assert response.status_code == 200 and response.json()["status"] == "failed", (
        response.text,
        c.review_errors,
    )
    assert response.json()["result"] is None
    calls = await synthesis.execution.dispatch.coordination.calls(c)
    assert calls[-1]["status"] == "unknown" and calls[-1]["model_usage"] == model.usage.model_dump(
        mode="json"
    )
