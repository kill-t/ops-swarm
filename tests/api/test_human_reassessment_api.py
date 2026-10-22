"""真实人工准备工件经独立 Worker 重评，核验共享预算与迟到隔离。"""

import asyncio
import json
from datetime import timedelta
from uuid import uuid4

import httpx2
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from opsswarm.control_plane.app import create_app
from opsswarm.identity.settings import OperatorAuthSettings
from opsswarm.investigation.budget.contracts import CallGrant
from opsswarm.investigation.budget.model_contracts import ReportedModelUsage
from opsswarm.investigation.budget.queries import CallBudgetQueries
from opsswarm.investigation.budget.tables import CallReservationRow
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.human_prepare.contracts import PreparationArtifact
from opsswarm.investigation.human_review.contracts import HumanReviewError
from opsswarm.investigation.human_review.job_store import HumanReviewJobStore
from opsswarm.investigation.human_review.ledger import HumanReviewLedger
from opsswarm.investigation.human_review.model import HumanReviewModel
from opsswarm.investigation.human_review.store import HumanReviewStore
from opsswarm.investigation.human_review.tables import (
    HumanReviewCommandRow,
    HumanReviewJobRow,
    HumanReviewResultRow,
)
from opsswarm.investigation.human_review.worker import HumanReviewWorker
from opsswarm.investigation.review.contracts import HypothesisReviewProposal, ReviewModelResult
from tests.api.test_human_preparation_api import get as get_preparation
from tests.api.test_human_preparation_api import prepare
from tests.api.test_human_preparation_api import worker as preparation_worker
from tests.api.test_human_wait_api import command, post
from tests.api.test_human_wait_api import wait_context as wait_context
from tests.identity_helpers import auth_header
from tests.model_budget_helpers import sdk_model, sdk_response

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def review_result(**changes):
    return ReviewModelResult(
        proposal=HypothesisReviewProposal(
            **{
                "support_is_sufficient": False,
                "rationale": "人工声明保留为待核实材料，本轮不产生执行授权。",
                "evidence_gaps": ("还需核实连接池变化",),
                "suggested_direction": "human_review",
                **changes,
            }
        )
    )


class RecordingModel:
    def __init__(self, *, result=None, callback=None):
        self.inputs = []
        self.usage = None
        self.result = result or review_result()
        self.callback = callback

    async def invoke(self, supplied):
        self.inputs.append(supplied)
        self.usage = ReportedModelUsage(input_tokens=80, output_tokens=20, total_tokens=100)
        if self.callback is not None:
            await self.callback()
        return self.result


async def prepared_context(c):
    response = await post(c, command(c))
    assert response.status_code == 201, response.text
    created = await prepare(c)
    assert created.status_code == 201, created.text
    preparation_id = created.json()["job"]["id"]
    assert await preparation_worker(c).run_once()
    prepared = await get_preparation(c, preparation_id)
    assert prepared.status_code == 200, prepared.text
    artifact = PreparationArtifact.model_validate(prepared.json()["artifact"])
    assert artifact.diagnostic.status == "ready"
    assert prepared.json()["artifact_sha256"] == content_hash(artifact)
    c.app.state.human_review_store = HumanReviewStore(c.db, clock=c.clock)
    c.preparation_id, c.artifact = preparation_id, artifact
    c.review_base = f"{c.base}/preparations/{preparation_id}/reassessments"


def body(c, *, key="reassessment-command-001", **changes):
    return {
        **command(c, action="reassess", version=2, key=key),
        "preparation_sha256": content_hash(c.artifact),
        **changes,
    }


async def reassess(c, payload=None, *, header=None, base=None):
    return await c.client.post(
        base or c.review_base,
        json=body(c) if payload is None else payload,
        headers=header or auth_header("engineer"),
    )


async def get(c, job_id):
    return await c.client.get(f"{c.review_base}/{job_id}", headers=auth_header("viewer"))


def worker(c, model):
    return HumanReviewWorker(
        HumanReviewJobStore(c.db, clock=c.clock),
        HumanReviewLedger(c.db, clock=c.clock),
        model_factory=lambda policy: model,
        worker_id="human-review-worker",
    )


async def count(c, table):
    async with c.db() as session:
        return await session.scalar(select(func.count()).select_from(table))


async def test_reassessment_sdk_consumes_exact_input_and_preserves_old_work_and_facts(wait_context):
    c = wait_context
    await prepared_context(c)
    before = await CallBudgetQueries(c.db).get_snapshot(c.started.run.id)
    old_work = await c.store.get_work_item(c.started.work_item.id)
    old_attempts = await c.store.list_attempts(c.started.run.id)
    requests = []

    def reply(request):
        payload = json.loads(request.content)
        requests.append(payload)
        return sdk_response(payload, review_result().proposal.model_dump(mode="json"))

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(reply)) as model_client:
        model = HumanReviewModel(sdk_model(model_client), before.model_budget)
        created = await reassess(c)
        assert created.status_code == 201, created.text
        job_id = created.json()["job"]["id"]
        assert await worker(c, model).run_once()
        assert not await worker(c, model).run_once()
    response = await get(c, job_id)
    assert response.status_code == 200, response.text
    view = response.json()
    assert view["status"] == "succeeded" and view["availability"] == "current"
    assert view["result"]["content"] == review_result().model_dump(mode="json")
    assert view["result"]["content_sha256"] == content_hash(review_result())
    assert len(requests) == 1
    supplied = c.artifact.prepared_input.model_input
    assert requests[0]["messages"] == [m.model_dump() for m in supplied.messages]
    assert requests[0]["max_completion_tokens"] == before.model_budget.max_output_tokens_per_call
    after = await CallBudgetQueries(c.db).get_snapshot(c.started.run.id)
    assert after.calls[:-1] == before.calls
    assert after.reserved_model_calls == before.reserved_model_calls + 1
    assert after.reserved_tool_calls == before.reserved_tool_calls
    assert (after.policy, after.model_budget, after.started_at, after.deadline) == (
        before.policy,
        before.model_budget,
        before.started_at,
        before.deadline,
    )
    call = after.calls[-1]
    assert call.operation == "human_review" and call.status == "succeeded"
    assert str(call.human_review_attempt_id) == view["attempts"][0]["id"]
    assert call.investigation_attempt_id == c.handoff.investigation_attempt_id
    assert str(call.id) == view["result"]["source_call_id"]
    assert call.model_usage.total_tokens == 100
    for suffix, expected in [
        ("model-input", supplied.model_dump(mode="json")),
        ("context-manifest", c.artifact.prepared_input.manifest.model_dump(mode="json")),
    ]:
        trace = await c.client.get(
            f"/api/v1/investigations/{c.started.run.id}/calls/{call.id}/{suffix}",
            headers=auth_header("viewer"),
        )
        assert trace.status_code == 200, trace.text
        assert trace.json() == expected
    interaction = await c.client.get(
        f"/api/v1/investigations/{c.started.run.id}/calls/{call.id}/interaction",
        headers=auth_header("viewer"),
    )
    assert interaction.status_code == 200, interaction.text
    assert interaction.json()["result"] == review_result().model_dump(mode="json")
    catalog = await c.client.get(
        f"/api/v1/investigations/{c.started.run.id}/calls/{call.id}/evidence-catalog",
        headers=auth_header("viewer"),
    )
    assert catalog.status_code == 409, catalog.text
    assert all(
        "human_review_attempt_id" not in item.model_dump(mode="json") for item in before.calls
    )
    async with c.db() as session:
        row = await session.get(CallReservationRow, call.id)
        assert row.model_input == supplied.model_dump(mode="json")
        assert row.context_manifest == c.artifact.prepared_input.manifest.model_dump(mode="json")
        assert row.model_allocation == c.artifact.prepared_input.allocation.model_dump(mode="json")
    assert await c.store.get_work_item(c.started.work_item.id) == old_work
    assert await c.store.list_attempts(c.started.run.id) == old_attempts
    assert (await c.store.get_run(c.started.run.id)).status == "await_human"
    assert (await get_preparation(c, c.preparation_id)).json()["artifact"] == c.artifact.model_dump(
        mode="json"
    )
    for table in [
        "investigation_human_review_jobs",
        "investigation_human_review_attempts",
        "investigation_human_review_results",
        "investigation_human_review_commands",
    ]:
        with pytest.raises(DBAPIError):
            async with c.db() as session, session.begin():
                await session.execute(text(f"DELETE FROM {table}"))
    assert (await post(c, command(c, version=2, key="later-material-001"))).status_code == 201
    historical = (await get(c, job_id)).json()
    assert historical["result"] == view["result"] and historical["availability"] == "stale"
    assert (await reassess(c)).status_code == 200
    async with c.db() as session, session.begin():
        await session.execute(
            text("UPDATE operator_authorizations SET body_sha256=:hash WHERE id=:id"),
            {"hash": "d" * 64, "id": view["authorization_id"]},
        )
    assert (await get(c, job_id)).status_code == 409


async def test_reassessment_trust_strict_body_business_audits_and_creation_race(wait_context):
    c = wait_context
    await prepared_context(c)
    for path in [f"{c.review_base}/commands", f"{c.review_base}/{uuid4()}"]:
        assert (await c.client.get(path)).status_code == 401
    assert (await c.client.post(c.review_base, json=body(c))).status_code == 401
    legacy = create_app(operator_auth_settings=OperatorAuthSettings(mode="legacy_local"))
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=legacy), base_url="http://test"
    ) as client:
        assert (await client.post(c.review_base, json=body(c))).status_code == 401
        assert (await client.get(f"{c.review_base}/commands")).status_code == 401
    for role in ["viewer", "approver", "executor", "alert_source"]:
        assert (await reassess(c, header=auth_header(role))).status_code == 403
    for change in [
        {"actor_id": "forged"},
        {"authorization_id": str(uuid4())},
        {"expected_wait_version": True},
        {"action": "resume"},
        {"items": []},
    ]:
        assert (await reassess(c, body(c, **change))).status_code == 422
    oversized = await c.client.post(
        c.review_base,
        content=json.dumps(body(c)) + " " * 65536,
        headers={**auth_header("engineer"), "Content-Type": "application/json"},
    )
    assert oversized.status_code == 413
    unknown_run = (
        f"/api/v1/investigations/{uuid4()}/human-wait/preparations/{c.preparation_id}/reassessments"
    )
    unknown_preparation = f"{c.base}/preparations/{uuid4()}/reassessments"
    assert (await reassess(c, base=unknown_run)).status_code == 404
    assert (await reassess(c, base=unknown_preparation)).status_code == 404
    for change in [
        {"handoff_id": str(uuid4())},
        {"source_sha256": "a" * 64},
        {"expected_wait_version": 3},
        {"preparation_sha256": "b" * 64},
    ]:
        assert (await reassess(c, body(c, **change))).status_code == 409
    results = await asyncio.gather(reassess(c), reassess(c, body(c, key="racing-review-002")))
    assert sorted(r.status_code for r in results) == [201, 409], [r.text for r in results]
    winner = next(r.json()["job"] for r in results if r.status_code == 201)
    applied_key = next(
        body(c)["idempotency_key"] if i == 0 else "racing-review-002"
        for i, r in enumerate(results)
        if r.status_code == 201
    )
    alternate = body(c, key=applied_key)
    alternate.pop("action")
    alternate["reason"] = f"  {alternate['reason']}  "
    replay = await reassess(c, alternate)
    assert replay.status_code == 200 and replay.json()["job"]["id"] == winner["id"]
    assert (await reassess(c, body(c, key=applied_key, reason="改变请求内容"))).status_code == 409
    assert (await get(c, uuid4())).status_code == 404
    assert await count(c, HumanReviewJobRow) == 1
    async with c.db() as session:
        failures = await session.scalar(
            select(func.count())
            .select_from(HumanReviewCommandRow)
            .where(HumanReviewCommandRow.outcome == "error")
        )
    assert failures == 8
    page = await c.client.get(f"{c.review_base}/commands?limit=1", headers=auth_header("viewer"))
    assert page.status_code == 200 and page.json()["next_cursor"] is not None
    assert "raw_body" not in page.text


async def test_reassessment_original_deadline_blocks_worker_without_new_reservation(wait_context):
    c = wait_context
    await prepared_context(c)
    created = await reassess(c)
    assert created.status_code == 201, created.text
    before = await CallBudgetQueries(c.db).get_snapshot(c.started.run.id)
    c.clock.current = before.deadline + timedelta(seconds=1)
    model = RecordingModel()
    assert await worker(c, model).run_once()
    assert model.inputs == []
    view = (await get(c, created.json()["job"]["id"])).json()
    assert view["status"] == "failed" and view["result"] is None
    assert view["last_error_code"] == "run_deadline_exceeded"
    assert await CallBudgetQueries(c.db).get_snapshot(c.started.run.id) == before


@pytest.mark.parametrize("change", ["supplement", "cancel"])
async def test_inflight_wait_change_records_unknown_and_never_publishes_result(
    wait_context, change
):
    c = wait_context
    await prepared_context(c)
    created = await reassess(c)
    assert created.status_code == 201, created.text
    entered, release = asyncio.Event(), asyncio.Event()

    async def pause():
        entered.set()
        await release.wait()

    model = RecordingModel(callback=pause)
    running = asyncio.create_task(worker(c, model).run_once())
    await asyncio.wait_for(entered.wait(), 30)
    try:
        altered = await post(c, command(c, action=change, version=2, key="during-review-001"))
        assert altered.status_code == 201, altered.text
    finally:
        release.set()
    assert await running
    snapshot = await CallBudgetQueries(c.db).get_snapshot(c.started.run.id)
    assert snapshot.calls[-1].status == "unknown"
    assert snapshot.calls[-1].model_usage.total_tokens == 100
    assert snapshot.calls[-1].human_review_attempt_id is not None
    assert await count(c, HumanReviewResultRow) == 0
    view = (await get(c, created.json()["job"]["id"])).json()
    assert view["status"] == "failed" and view["availability"] == "stale"
    historic = await c.client.get(
        f"/api/v1/investigations/{c.started.run.id}/terminal-handoff",
        headers=auth_header("viewer"),
    )
    assert historic.status_code == 200, historic.text
    assert historic.json() == c.handoff.model_dump(mode="json")


async def test_expired_attempt_keeps_unknown_and_new_attempt_uses_same_budget(wait_context):
    c = wait_context
    await prepared_context(c)
    created = await reassess(c)
    assert created.status_code == 201, created.text
    jobs, ledger = HumanReviewJobStore(c.db, clock=c.clock), HumanReviewLedger(c.db, clock=c.clock)
    old = await jobs.claim_next(worker_id="lost-worker", lease_duration=timedelta(seconds=3))
    grants = await asyncio.gather(ledger.reserve(old), ledger.reserve(old), return_exceptions=True)
    assert sum(isinstance(item, CallGrant) for item in grants) == 1
    assert sum(isinstance(item, HumanReviewError) for item in grants) == 1
    reserved = next(item for item in grants if isinstance(item, CallGrant))
    before = await CallBudgetQueries(c.db).get_snapshot(c.started.run.id)
    assert before.calls[-1].status == "reserved"
    c.clock.current += timedelta(seconds=4)
    model = RecordingModel()
    assert await worker(c, model).run_once()
    assert len(model.inputs) == 1
    view = (await get(c, created.json()["job"]["id"])).json()
    assert view["status"] == "succeeded"
    assert [a["status"] for a in view["attempts"]] == ["lease_expired", "succeeded"]
    assert len({a["id"] for a in view["attempts"]}) == 2
    after = await CallBudgetQueries(c.db).get_snapshot(c.started.run.id)
    assert after.calls[-2].id == reserved.record.id and after.calls[-2].status == "unknown"
    assert after.calls[-2].model_usage is None
    assert after.calls[-1].status == "succeeded"
    assert after.reserved_model_calls == before.reserved_model_calls + 1
    assert after.model_usage.unknown_model_calls == before.model_usage.unknown_model_calls
    assert after.model_usage.committed_input_tokens > before.model_usage.committed_input_tokens
    with pytest.raises(HumanReviewError, match="租约"):
        await ledger.settle(
            old, reserved.record.id, status="succeeded", usage=None, result=review_result()
        )
    assert await CallBudgetQueries(c.db).get_snapshot(c.started.run.id) == after


async def test_successful_call_is_reused_after_result_commit_crash(wait_context):
    c = wait_context
    await prepared_context(c)
    created = await reassess(c)
    assert created.status_code == 201, created.text
    jobs, ledger = HumanReviewJobStore(c.db, clock=c.clock), HumanReviewLedger(c.db, clock=c.clock)
    old = await jobs.claim_next(
        worker_id="crashed-after-settle", lease_duration=timedelta(seconds=3)
    )
    grant = await ledger.reserve(old)
    assert await ledger.settle(
        old, grant.record.id, status="succeeded", usage=None, result=review_result()
    )
    settled = await CallBudgetQueries(c.db).get_snapshot(c.started.run.id)
    c.clock.current += timedelta(seconds=4)
    model = RecordingModel()
    assert await worker(c, model).run_once()
    assert model.inputs == []
    view = (await get(c, created.json()["job"]["id"])).json()
    assert view["status"] == "succeeded"
    assert [a["status"] for a in view["attempts"]] == ["lease_expired", "succeeded"]
    assert view["result"]["source_call_id"] == str(grant.record.id)
    assert await CallBudgetQueries(c.db).get_snapshot(c.started.run.id) == settled


async def test_human_event_id_cannot_become_evidence_reference(wait_context):
    c = wait_context
    await prepared_context(c)
    created = await reassess(c)
    assert created.status_code == 201, created.text
    human_id = c.artifact.prepared_input.manifest.materials[-1].material_id
    model = RecordingModel(result=review_result(counter_evidence_ids=(str(human_id),)))
    assert await worker(c, model).run_once()
    assert len(model.inputs) == 1
    assert await count(c, HumanReviewResultRow) == 0
    view = (await get(c, created.json()["job"]["id"])).json()
    assert view["status"] == "failed" and view["result"] is None
    assert (await CallBudgetQueries(c.db).get_snapshot(c.started.run.id)).calls[
        -1
    ].status != "succeeded"


async def test_repeated_infrastructure_failure_is_limited_to_three_attempts(wait_context):
    c = wait_context
    await prepared_context(c)
    created = await reassess(c)
    assert created.status_code == 201, created.text

    class UnclassifiedTransportError(RuntimeError):
        code = None

    async def fail():
        raise UnclassifiedTransportError("假模型传输故障")

    model = RecordingModel(callback=fail)
    w = worker(c, model)
    before = await CallBudgetQueries(c.db).get_snapshot(c.started.run.id)
    for _ in range(3):
        assert await w.run_once()
        c.clock.current += timedelta(seconds=3)
    assert not await w.run_once()
    assert len(model.inputs) == 3
    view = (await get(c, created.json()["job"]["id"])).json()
    assert view["status"] == "failed" and len(view["attempts"]) == 3
    assert all(a["status"] == "failed" for a in view["attempts"])
    assert await count(c, HumanReviewResultRow) == 0
    after = await CallBudgetQueries(c.db).get_snapshot(c.started.run.id)
    assert after.reserved_model_calls == before.reserved_model_calls + 3


async def test_reported_overallocation_remains_in_ledger_without_business_result(wait_context):
    c = wait_context
    await prepared_context(c)
    created = await reassess(c)
    assert created.status_code == 201, created.text
    model = RecordingModel()

    async def overspend():
        model.usage = ReportedModelUsage(
            input_tokens=1000000, output_tokens=20, total_tokens=1000020
        )

    model.callback = overspend
    assert await worker(c, model).run_once()
    view = (await get(c, created.json()["job"]["id"])).json()
    assert view["status"] == "failed" and view["result"] is None
    last = (await CallBudgetQueries(c.db).get_snapshot(c.started.run.id)).calls[-1]
    assert last.status == "succeeded" and last.model_usage.input_tokens == 1000000
    assert await count(c, HumanReviewResultRow) == 0


async def test_deadline_passed_during_model_keeps_unknown_usage_without_result(wait_context):
    c = wait_context
    await prepared_context(c)
    created = await reassess(c)
    before = await CallBudgetQueries(c.db).get_snapshot(c.started.run.id)
    model = RecordingModel()

    # 让原预算先于本次新租约到期，单独验证运行时限而非租约接管。
    c.clock.current = before.started_at + timedelta(seconds=400)

    async def expire():
        c.clock.current = before.deadline + timedelta(seconds=1)

    model.callback = expire
    assert await worker(c, model).run_once()
    view = (await get(c, created.json()["job"]["id"])).json()
    assert view["status"] == "failed" and view["result"] is None
    after = await CallBudgetQueries(c.db).get_snapshot(c.started.run.id)
    assert after.calls[-1].status == "unknown"
    assert after.calls[-1].model_usage.total_tokens == 100


async def test_reassessment_auth_expiry_after_written_audit_rolls_back(wait_context, monkeypatch):
    import opsswarm.investigation.human_review.store as store_module
    from tests.task_facts_helpers import BASE_TIME

    c = wait_context
    await prepared_context(c)
    original = store_module.read_job

    async def expired_view(*args, **kwargs):
        result = await original(*args, **kwargs)
        c.clock.current = BASE_TIME + timedelta(days=1)
        return result

    monkeypatch.setattr(store_module, "read_job", expired_view)
    response = await reassess(c)
    assert response.status_code == 401, response.text
    assert await count(c, HumanReviewJobRow) == await count(c, HumanReviewCommandRow) == 0


async def test_result_flush_then_lease_expiry_rolls_back_business_success(
    wait_context, monkeypatch
):
    import opsswarm.investigation.human_review.job_store as job_module

    c = wait_context
    await prepared_context(c)
    created = await reassess(c)
    jobs, ledger = HumanReviewJobStore(c.db, clock=c.clock), HumanReviewLedger(c.db, clock=c.clock)
    claim = await jobs.claim_next(worker_id="expiring-result", lease_duration=timedelta(seconds=3))
    grant = await ledger.reserve(claim)
    assert await ledger.settle(
        claim, grant.record.id, status="succeeded", usage=None, result=review_result()
    )
    original = job_module._finish_job

    def expire(*args, **kwargs):
        original(*args, **kwargs)
        c.clock.current += timedelta(seconds=4)

    monkeypatch.setattr(job_module, "_finish_job", expire)
    with pytest.raises(HumanReviewError, match="提交阶段租约已过期"):
        await jobs.complete(claim, grant.record.id)
    assert await count(c, HumanReviewResultRow) == 0
    assert (await get(c, created.json()["job"]["id"])).json()["status"] == "running"
    assert (await CallBudgetQueries(c.db).get_snapshot(c.started.run.id)).calls[
        -1
    ].status == "succeeded"
