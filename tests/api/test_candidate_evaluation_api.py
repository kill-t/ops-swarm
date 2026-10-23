"""双来源候选评估的可信命令、原预算、租约和持久结果。"""

import asyncio
import copy
import json
from datetime import timedelta
from uuid import UUID, uuid4

import httpx2
import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

import opsswarm.investigation.candidate.job_store as candidate_jobs
from opsswarm.investigation.budget.queries import CallBudgetQueries
from opsswarm.investigation.candidate.contracts import CandidateError
from opsswarm.investigation.candidate.job_store import CandidateJobStore
from opsswarm.investigation.candidate.ledger import CandidateLedger
from opsswarm.investigation.candidate.model import CandidateModel
from opsswarm.investigation.candidate.output import (
    CandidateEvaluation,
    CandidateModelResult,
    CandidateProposal,
)
from opsswarm.investigation.candidate.store import CandidateStore
from opsswarm.investigation.candidate.tables import (
    CandidateAttemptRow,
    CandidateJobRow,
    CandidateResultRow,
)
from opsswarm.investigation.candidate.worker import CandidateWorker
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.human_routing.contracts import HumanRoutingRecord
from tests.api.test_human_reassessment_api import (
    RecordingModel,
    count,
    prepared_context,
    reassess,
)
from tests.api.test_human_reassessment_api import (
    worker as review_worker,
)
from tests.api.test_human_routing_api import proposal, route
from tests.api.test_human_wait_api import command, post
from tests.api.test_human_wait_api import wait_context as wait_context
from tests.identity_helpers import auth_header
from tests.model_budget_helpers import sdk_model, sdk_response

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def unsupported():
    return CandidateModelResult(
        proposal=CandidateProposal(
            status="unsupported",
            rationale="现有证据不足以确定白名单候选",
            unresolved_questions=("请工程师确认变更参数",),
        )
    )


class CandidateReplies(RecordingModel):
    def __init__(self, *, callback=None):
        super().__init__(result=unsupported(), callback=callback)


async def context(c, kind="terminal_handoff"):
    c.app.state.candidate_store = CandidateStore(c.db, clock=c.clock)
    c.candidate_base = f"/api/v1/investigations/{c.started.run.id}/candidate-evaluations"
    reference = c.handoff
    if kind == "human_route":
        await prepared_context(c, items=[{"kind": "statement", "text": "请继续核对候选依据"}])
        created = await reassess(c)
        assert created.status_code == 201, created.text
        assert await review_worker(
            c, RecordingModel(result=await proposal(c, "candidate"))
        ).run_once()
        response = await route(c, created.json()["job"]["id"])
        assert response.status_code == 200, response.text
        reference = response.json()["record"]
        assert reference["decision"]["route"] == "candidate_evaluation"
    c.candidate_command = {
        "action": "evaluate_candidate",
        "source_kind": kind,
        "source_id": str(reference.id) if kind == "terminal_handoff" else reference["id"],
        "source_sha256": content_hash(
            reference
            if kind == "terminal_handoff"
            else HumanRoutingRecord.model_validate(reference)
        ),
        "expected_wait_version": None if kind == "terminal_handoff" else reference["wait_version"],
        "idempotency_key": "candidate-command-116",
        "reason": "工程师请求只读候选评估",
    }


async def evaluate(c, body=None, *, header=None):
    return await c.client.post(
        c.candidate_base,
        json=body or c.candidate_command,
        headers=header or auth_header("engineer"),
    )


async def get_candidate(c, job_id):
    return await c.client.get(f"{c.candidate_base}/{job_id}", headers=auth_header("viewer"))


def worker(c, model):
    return CandidateWorker(
        CandidateJobStore(c.db, clock=c.clock),
        CandidateLedger(c.db, clock=c.clock),
        model_factory=lambda policy: model,
        worker_id="candidate-test-worker",
    )


@pytest.mark.parametrize(
    ("wait_context", "kind"),
    [("candidate", "terminal_handoff"), ("human_clean", "human_route")],
    indirect=["wait_context"],
)
async def test_real_sdk_preserves_exact_source_original_budget_and_parent(wait_context, kind):
    c = wait_context
    await context(c, kind)
    before = await CallBudgetQueries(c.db).get_snapshot(c.started.run.id)
    old_work = await c.store.get_work_item(c.started.work_item.id)
    old_attempts = await c.store.list_attempts(c.started.run.id)
    requests = []

    def reply(request):
        payload = json.loads(request.content)
        requests.append(payload)
        return sdk_response(payload, unsupported().proposal.model_dump(mode="json"))

    created = await evaluate(c)
    assert created.status_code == 201, created.text
    job_id = created.json()["job"]["id"]
    async with c.db() as session:
        job = await session.get(CandidateJobRow, UUID(job_id))
        supplied = copy.deepcopy(job.packet)
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(reply)) as client:
        model = CandidateModel(sdk_model(client), before.model_budget)
        assert await worker(c, model).run_once()
        assert not await worker(c, model).run_once()
    response = await get_candidate(c, job_id)
    assert response.status_code == 200, response.text
    view = response.json()
    assert view["status"] == "succeeded" and view["availability"] == "current"
    result = view["result"]["content"]
    assert result["execution_eligible"] is False and result["disposition"] == "await_human"
    assert result["proposal"] == unsupported().proposal.model_dump(mode="json")
    assert result["reason_code"] == "candidate_unsupported"
    assert (
        content_hash(CandidateEvaluation.model_validate(result)) == view["result"]["content_sha256"]
    )
    assert len(requests) == 1 and requests[0]["messages"] == supplied["model_input"]["messages"]
    after = await CallBudgetQueries(c.db).get_snapshot(c.started.run.id)
    assert after.calls[:-1] == before.calls
    assert after.reserved_model_calls == before.reserved_model_calls + 1
    assert (after.policy, after.model_budget, after.started_at, after.deadline) == (
        before.policy,
        before.model_budget,
        before.started_at,
        before.deadline,
    )
    call = after.calls[-1]
    assert call.operation == "evaluate_candidate" and call.status == "succeeded"
    assert call.candidate_attempt_id == UUID(view["attempts"][0]["id"])
    assert call.human_review_attempt_id is None
    assert call.investigation_attempt_id == c.claimed.attempt.id
    queries = CallBudgetQueries(c.db)
    assert (await queries.get_model_input(c.started.run.id, call.id)).model_dump(
        mode="json"
    ) == supplied["model_input"]
    assert (await queries.get_context_manifest(c.started.run.id, call.id)).model_dump(
        mode="json"
    ) == supplied["manifest"]
    assert (await queries.get_interaction(c.started.run.id, call.id)).result == unsupported()
    assert await c.store.get_work_item(c.started.work_item.id) == old_work
    assert await c.store.list_attempts(c.started.run.id) == old_attempts
    assert (await c.store.get_run(c.started.run.id)).status == (
        "await_candidate" if kind == "terminal_handoff" else "await_human"
    )
    replay = await evaluate(c)
    assert replay.status_code == 200 and replay.json()["applied"] is False
    assert replay.json()["job"]["result"] == view["result"]
    commands = await c.client.get(f"{c.candidate_base}/commands", headers=auth_header("viewer"))
    assert [r["outcome"] for r in commands.json()["items"]] == ["applied", "replayed"]
    assert await count(c, CandidateJobRow) == await count(c, CandidateResultRow) == 1


@pytest.mark.parametrize("wait_context", ["candidate"], indirect=True)
async def test_trusted_permissions_full_command_idempotency_and_audit(wait_context):
    c = wait_context
    await context(c)
    assert (await c.client.post(c.candidate_base, json=c.candidate_command)).status_code == 401
    for role in ("viewer", "approver", "executor", "alert_source"):
        response = await evaluate(c, header=auth_header(role))
        assert response.status_code == 403, response.text
    results = await asyncio.gather(evaluate(c), evaluate(c))
    assert sorted(r.status_code for r in results) == [200, 201]
    job_id = results[0].json()["job"]["id"]
    for changed in ({"reason": "改动完整命令"}, {"source_id": str(uuid4())}):
        response = await evaluate(c, {**c.candidate_command, **changed})
        assert response.status_code == 409
    assert (await c.client.get(f"{c.candidate_base}/{job_id}")).status_code == 401
    assert (await get_candidate(c, uuid4())).status_code == 404
    assert (await get_candidate(c, job_id)).json()["status"] == "queued"
    assert await count(c, CandidateAttemptRow) == 0


@pytest.mark.parametrize("wait_context", ["candidate"], indirect=True)
@pytest.mark.parametrize("change", [{"source_sha256": "0" * 64}, {"source_id": str(uuid4())}])
async def test_invalid_source_is_business_audited_without_job(wait_context, change):
    c = wait_context
    await context(c)
    response = await evaluate(c, {**c.candidate_command, **change})
    assert response.status_code in (404, 409), response.text
    assert await count(c, CandidateJobRow) == 0
    audits = await c.client.get(f"{c.candidate_base}/commands", headers=auth_header("viewer"))
    assert audits.status_code == 200 and audits.json()["items"][0]["outcome"] == "error"


@pytest.mark.parametrize("wait_context", ["human_clean"], indirect=True)
@pytest.mark.parametrize("action", ["supplement", "cancel"])
async def test_changed_human_prefix_rejects_publication_but_records_usage(wait_context, action):
    c = wait_context
    await context(c, "human_route")
    created = await evaluate(c)
    assert created.status_code == 201, created.text

    async def change():
        response = await post(c, command(c, action=action, version=2, key="candidate-change-116"))
        assert response.status_code == 201, response.text

    model = CandidateReplies(callback=change)
    assert await worker(c, model).run_once()
    view = (await get_candidate(c, created.json()["job"]["id"])).json()
    assert view["availability"] == "stale" and view["status"] == "failed"
    assert view["result"] is None
    call = (await CallBudgetQueries(c.db).get_snapshot(c.started.run.id)).calls[-1]
    assert call.status == "unknown" and call.model_usage is not None
    assert len(model.inputs) == 1
    assert (await evaluate(c)).status_code == 200
    assert not await worker(c, model).run_once()


@pytest.mark.parametrize("wait_context", ["candidate"], indirect=True)
async def test_exact_deadline_and_lease_expiration_never_publish(wait_context):
    c = wait_context
    await context(c)
    created = await evaluate(c)
    assert created.status_code == 201, created.text
    jobs = CandidateJobStore(c.db, clock=c.clock)
    ledger = CandidateLedger(c.db, clock=c.clock)
    claimed = await jobs.claim_next(worker_id="candidate-a", lease_duration=timedelta(seconds=20))
    assert claimed is not None
    grant = await ledger.reserve(claimed)
    assert not grant.created or grant.record.candidate_attempt_id == claimed.attempt_id
    c.clock.current += timedelta(seconds=20)
    with pytest.raises(CandidateError):
        await ledger.settle(
            claimed, grant.record.id, status="succeeded", result=unsupported(), usage=None
        )
    reclaimed = await jobs.claim_next(worker_id="candidate-b")
    assert reclaimed is not None and reclaimed.attempt_id != claimed.attempt_id
    old = (await CallBudgetQueries(c.db).get_snapshot(c.started.run.id)).calls[-1]
    assert old.status == "unknown" and old.model_usage is None
    with pytest.raises(CandidateError):
        await jobs.complete(claimed, grant.record.id)


@pytest.mark.parametrize("wait_context", ["candidate"], indirect=True)
async def test_deadline_does_not_reset_on_command_or_worker(wait_context):
    c = wait_context
    await context(c)
    c.clock.current += timedelta(seconds=901)
    response = await evaluate(c)
    assert response.status_code == 409
    assert await count(c, CandidateJobRow) == 0


@pytest.mark.parametrize("wait_context", ["candidate"], indirect=True)
async def test_successful_call_reused_after_result_transaction_failure(wait_context, monkeypatch):
    c = wait_context
    await context(c)
    created = await evaluate(c)
    assert created.status_code == 201, created.text
    original = candidate_jobs.CandidateJobStore.complete
    failed = False

    async def fail_once(self, *args, **kwargs):
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError("模拟结果事务暂时失败")
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(candidate_jobs.CandidateJobStore, "complete", fail_once)
    model = CandidateReplies()
    assert await worker(c, model).run_once()
    c.clock.current += timedelta(seconds=3)
    assert await worker(c, model).run_once()
    response = await get_candidate(c, created.json()["job"]["id"])
    assert response.json()["status"] == "succeeded", response.text
    assert len(model.inputs) == 1 and len(response.json()["attempts"]) == 2
    assert await count(c, CandidateResultRow) == 1


@pytest.mark.parametrize("wait_context", ["candidate"], indirect=True)
async def test_forged_frozen_material_fails_closed_and_native_source_is_immutable(wait_context):
    c = wait_context
    await context(c)
    created = await evaluate(c)
    assert created.status_code == 201, created.text
    job_id = UUID(created.json()["job"]["id"])
    async with c.db() as session:
        with pytest.raises(DBAPIError):
            await session.execute(
                text(
                    "UPDATE investigation_candidate_evaluation_jobs "
                    "SET source_sha256=:hash WHERE id=:id"
                ),
                {"hash": "0" * 64, "id": job_id},
            )
            await session.commit()
    assert (await get_candidate(c, job_id)).status_code == 200


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_supported_whitelist_candidate_has_deterministic_step_but_no_execution(wait_context):
    c = wait_context
    await context(c)
    created = await evaluate(c)
    assert created.status_code == 201, created.text
    job_id = UUID(created.json()["job"]["id"])
    async with c.db() as session:
        job = await session.get(CandidateJobRow, job_id)
        evidence_ids = [e["evidence_id"] for e in job.source["evidence"]]
    assert len(evidence_ids) > 1
    model = RecordingModel(
        result=CandidateModelResult(
            proposal=CandidateProposal(
                status="candidate",
                action_type="rollback_release",
                rationale="仅评估明确版本的回滚候选",
                evidence_ids=evidence_ids,
                preconditions=("由工程师复核后另行规划及审批",),
            )
        )
    )
    assert await worker(c, model).run_once()
    response = await get_candidate(c, job_id)
    assert response.status_code == 200, response.text
    view = response.json()
    assert view["status"] == "succeeded", response.text
    result = view["result"]["content"]
    assert result["disposition"] == "planning_review" and result["execution_eligible"] is False
    assert result["step"]["action_type"] == "rollback_release"
    assert result["step"]["parameters"]["target_version"]
    assert result["step"]["verification"]
    async with c.db() as session:
        for table in ("action_plans", "approval_requests", "operation_executions"):
            assert await session.scalar(text(f"SELECT count(*) FROM {table}")) == 0


@pytest.mark.parametrize("wait_context", ["candidate"], indirect=True)
@pytest.mark.parametrize(
    "mode", ["foreign_evidence", "oversized_usage", "unknown_usage", "incident_closed"]
)
async def test_post_call_guards_and_unknown_usage_remain_conservative(wait_context, mode):
    c = wait_context
    await context(c)
    created = await evaluate(c)
    assert created.status_code == 201, created.text
    model = CandidateReplies()
    if mode == "foreign_evidence":
        model.result = CandidateModelResult(
            proposal=CandidateProposal(
                status="candidate",
                action_type="rollback_release",
                rationale="无效引用必须拒绝",
                evidence_ids=("foreign-evidence",),
                preconditions=("先核验",),
            )
        )
    original_invoke = model.invoke

    async def invoke(supplied):
        result = await original_invoke(supplied)
        if mode == "unknown_usage":
            model.usage = None
        if mode == "oversized_usage":
            from opsswarm.investigation.budget.model_contracts import ReportedModelUsage

            model.usage = ReportedModelUsage(
                input_tokens=9999999, output_tokens=20, total_tokens=10000019
            )
        if mode == "incident_closed":
            async with c.db() as session, session.begin():
                await session.execute(
                    text(
                        "UPDATE incidents SET status='closed', resolved_at=:now, "
                        "closed_at=:now, updated_at=:now WHERE id=:id"
                    ),
                    {"id": c.started.run.incident_id, "now": c.clock.current},
                )
        return result

    model.invoke = invoke
    assert await worker(c, model).run_once()
    view = (await get_candidate(c, created.json()["job"]["id"])).json()
    call = (await CallBudgetQueries(c.db).get_snapshot(c.started.run.id)).calls[-1]
    if mode == "unknown_usage":
        assert view["status"] == "succeeded" and call.model_usage is None
        snapshot = await CallBudgetQueries(c.db).get_snapshot(c.started.run.id)
        assert snapshot.model_usage.unknown_model_calls >= 1
    else:
        assert view["status"] == "failed" and view["result"] is None
        assert call.status in ("unknown", "failed") and call.model_usage is not None
    assert len(model.inputs) == 1


@pytest.mark.parametrize("wait_context", ["candidate"], indirect=True)
async def test_result_flush_then_lease_expiry_rolls_back_and_reuses_model_success(wait_context):
    c = wait_context
    await context(c)
    created = await evaluate(c)
    assert created.status_code == 201, created.text
    advanced = False

    def expire_after_result_flush(session, flush_context):
        nonlocal advanced
        if not advanced and any(isinstance(row, CandidateResultRow) for row in session.new):
            advanced = True
            c.clock.current += timedelta(seconds=601)

    model = CandidateReplies()
    event.listen(Session, "after_flush", expire_after_result_flush)
    try:
        assert await worker(c, model).run_once()
    finally:
        event.remove(Session, "after_flush", expire_after_result_flush)
    assert advanced
    job_id = created.json()["job"]["id"]
    response = await get_candidate(c, job_id)
    assert response.json()["status"] == "running" and response.json()["result"] is None
    assert await count(c, CandidateResultRow) == 0
    assert await worker(c, model).run_once()
    final = await get_candidate(c, job_id)
    assert final.json()["status"] == "succeeded", final.text
    assert [a["status"] for a in final.json()["attempts"]] == ["lease_expired", "succeeded"]
    assert len(model.inputs) == 1
    assert await count(c, CandidateResultRow) == 1


@pytest.mark.parametrize("wait_context", ["candidate"], indirect=True)
async def test_canonical_command_accepts_default_action_unicode_trim_and_uuid_forms(wait_context):
    c = wait_context
    await context(c)
    identifier = UUID(c.candidate_command["source_id"])
    padding = " \t\n\u3000"
    for index, source_id in enumerate(
        (f"urn:uuid:{str(identifier).upper()}", identifier.hex.upper(), "{" + str(identifier) + "}")
    ):
        body = {**c.candidate_command, "source_id": source_id}
        if index == 0:
            body.pop("action")
            for field in ("reason", "idempotency_key", "source_sha256"):
                body[field] = padding + body[field] + padding
        response = await evaluate(c, body)
        assert response.status_code == (201 if index == 0 else 200), response.text
        assert response.json()["job"]["source_id"] == str(identifier)
    commands = await c.client.get(f"{c.candidate_base}/commands", headers=auth_header("viewer"))
    assert [item["outcome"] for item in commands.json()["items"]] == [
        "applied",
        "replayed",
        "replayed",
    ]
