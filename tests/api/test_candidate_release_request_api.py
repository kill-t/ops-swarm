"""真实核验批准下的发布准备、只读版本观测与不可变审计。"""

import asyncio
import json
from datetime import datetime, timedelta
from uuid import UUID, uuid4, uuid5

import httpx2
import pytest
from sqlalchemy import event, text
from sqlalchemy.orm import Session

from tests.api import test_condition_approval_api as approval
from tests.api.test_candidate_conditions_api import canonical_hash, replace
from tests.api.test_human_wait_api import wait_context as wait_context
from tests.identity_helpers import auth_header, token

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


class RecordingObserver:
    def __init__(self, c):
        self.c, self.calls = c, 0
        self.error = None
        self.version = "2026.01.01-rc1"
        self.offset = 0

    async def observe(self):
        from opsswarm.execution.release.contracts import ReleaseObservation

        self.calls += 1
        if self.error is not None:
            raise self.error
        observed_at = self.c.clock() + timedelta(seconds=self.offset)
        return ReleaseObservation.model_validate(
            dict(
                observed_at=observed_at,
                source_path="/__admin/simulated-release",
                state=dict(
                    service_name="order-service",
                    source="local-release-simulation-v1",
                    simulation=True,
                    version=self.version,
                    revision=2,
                    command_count=1,
                    release_500_enabled=self.version == "2026.01.01-rc1",
                    updated_at=observed_at - timedelta(seconds=1),
                ),
            )
        )


async def ready(c, *, kind="terminal_handoff"):
    from opsswarm.execution.release.store import CandidateReleaseRequestStore

    await approval.ready(c, kind=kind)
    requested = await approval.create_request(c)
    assert requested.status_code == 201, requested.text
    decided = await approval.decide(
        c, approval.decision_body(c, requested.json()["request"]["record"])
    )
    assert decided.status_code == 201, decided.text
    c.release_approval = decided.json()["decision"]["record"]
    c.release_base = f"{c.conditions_base}/versions/1/release-execution-request"
    c.release_observer = RecordingObserver(c)
    c.app.state.candidate_release_request_store = CandidateReleaseRequestStore(
        c.db, observer=c.release_observer, clock=c.clock
    )
    c.release_body = dict(
        approval_decision_id=c.release_approval["id"],
        approval_content_sha256=c.release_approval["content_sha256"],
        expected_incident_version=c.release_approval["incident_version"],
        idempotency_key="candidate-release-request-124",
        reason="绑定当前核验批准与只读演练版本，尚不派发",
    )


async def post(c, body=None, *, headers=None, base=None):
    return await c.client.post(
        base or c.release_base,
        json=c.release_body if body is None else body,
        headers=headers or auth_header("executor"),
    )


async def get(c, suffix="", *, base=None):
    return await c.client.get((base or c.release_base) + suffix, headers=auth_header("viewer"))


async def counts(c):
    async with c.db() as session:
        return tuple(
            [
                await session.scalar(text(f"SELECT count(*) FROM {table}"))
                for table in (
                    "candidate_release_execution_requests",
                    "candidate_release_execution_commands",
                )
            ]
        )


class CountedTransport(httpx2.AsyncBaseTransport):
    def __init__(self, app):
        self.transport, self.calls = httpx2.ASGITransport(app=app), []

    async def handle_async_request(self, request):
        self.calls.append((request.method, request.url.path))
        return await self.transport.handle_async_request(request)

    async def aclose(self):
        await self.transport.aclose()


@pytest.mark.parametrize(
    ("wait_context", "kind"),
    [("candidate_with_change", "terminal_handoff"), ("human_clean_with_change", "human_route")],
    indirect=["wait_context"],
)
async def test_two_sources_bind_actual_lab_get_without_dispatch_or_parent_mutation(
    wait_context, kind
):
    from opsswarm.execution.release.observer import FaultLabReleaseObserver
    from opsswarm.execution.release.store import CandidateReleaseRequestStore
    from opsswarm.execution.settings import ExecutionSettings
    from opsswarm.fault_lab.app import create_app
    from opsswarm.fault_lab.controller import FaultController
    from opsswarm.fault_lab.release.store import LabReleaseStore

    c = wait_context
    await ready(c, kind=kind)
    lab_store = LabReleaseStore(c.db, clock=c.clock)
    await lab_store.initialize()
    await lab_store.set_legacy_fault(enabled=True)
    lab_app = create_app(FaultController(), lab_release_store=lab_store, runtime_clock=c.clock)
    transport = CountedTransport(lab_app)
    observer = FaultLabReleaseObserver(
        ExecutionSettings(fault_lab_base_url="http://order-service"),
        transport=transport,
        clock=c.clock,
    )
    c.app.state.candidate_release_request_store = CandidateReleaseRequestStore(
        c.db, observer=observer, clock=c.clock
    )
    before_parent, before_events = await approval.parent_facts(c), await approval.incident_events(c)
    before_lab = await lab_store.get()
    assert (await get(c)).json() == dict(
        record=None, availability="pending", requires_dispatch_recheck=True
    )
    result = await post(c)
    assert result.status_code == 201, result.text
    view = result.json()["request"]
    record, content = view["record"], view["record"]["content"]
    operation = uuid5(UUID(c.release_approval["id"]), "candidate-release-operation:v1")
    assert record["id"] == str(
        uuid5(UUID(c.release_approval["id"]), "candidate-release-request:v1")
    )
    assert record["operation_id"] == str(operation)
    assert record["status"] == "prepared" and view["availability"] == "current"
    assert view["requires_dispatch_recheck"] is True and content["dispatch_eligible"] is False
    assert content["version"] == "candidate-release-execution-request-content-v1"
    assert content["approval_decision_id"] == c.release_approval["id"]
    assert content["approval_content_sha256"] == c.release_approval["content_sha256"]
    assert content["verification_id"] == c.verification_record["id"]
    assert content["verification_content_sha256"] == c.verification_record["content_sha256"]
    assert content["plan_id"] == c.plan_record["id"]
    assert content["plan_content_sha256"] == c.plan_record["content_sha256"]
    assert content["step"] == c.plan_record["content"]["plan"]["steps"][0]
    assert content["cas_command"] == dict(
        expected_version="2026.01.01-rc1",
        expected_revision=2,
        target_version="stable",
        idempotency_key=f"candidate-release-{operation}",
    )
    assert record["content_sha256"] == canonical_hash(content)
    assert datetime.fromisoformat(record["expires_at"]) <= datetime.fromisoformat(
        c.verification_record["expires_at"]
    )
    assert (
        datetime.fromisoformat(record["expires_at"])
        - datetime.fromisoformat(content["observation"]["observed_at"])
    ).total_seconds() == 30
    assert (await get(c)).json() == view
    replay = await post(c)
    assert replay.status_code == 200 and replay.json() == dict(applied=False, request=view)
    assert transport.calls == [("GET", "/__admin/simulated-release")]
    assert await lab_store.get() == before_lab
    assert (
        await approval.parent_facts(c) == before_parent
        and await approval.incident_events(c) == before_events
    )
    assert c.plan_record["content"]["execution_eligible"] is False
    assert c.release_approval["content"]["execution_eligible"] is False
    async with c.db() as session:
        for table in ("operation_executions", "action_steps", "approval_requests"):
            assert await session.scalar(text(f"SELECT count(*) FROM {table}")) == 0
        assert await session.scalar(text("SELECT count(*) FROM lab_release_commands")) == 1
    page = (await get(c, "/commands?limit=1")).json()
    assert page["next_cursor"] is not None
    rest = (await get(c, f"/commands?after_cursor={page['next_cursor']}")).json()
    assert [i["outcome"] for i in page["items"] + rest["items"]] == ["applied", "replayed"]


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_concurrent_full_command_idempotency_and_other_subject_do_not_reobserve(wait_context):
    c = wait_context
    await ready(c)
    results = await asyncio.gather(post(c), post(c))
    assert sorted(r.status_code for r in results) == [200, 201]
    assert c.release_observer.calls == 1
    record = results[0].json()["request"]["record"]
    assert (await post(c, {**c.release_body, "reason": "同键不同理由"})).status_code == 409
    assert (
        await post(c, {**c.release_body, "idempotency_key": "another-request-124"})
    ).status_code == 409
    credential = next(i for i in c.settings.credentials if i.credential_id == "executor")
    c.app.state.operator_auth_settings = c.settings.model_copy(
        update={
            "credentials": (
                *c.settings.credentials,
                credential.model_copy(
                    update={"credential_id": "executor-two", "subject_id": "another-executor"}
                ),
            )
        }
    )
    other = {"Authorization": f"Bearer executor-two.{token('executor').split('.')[1]}"}
    assert (await post(c, headers=other)).status_code == 409
    assert c.release_observer.calls == 1 and (await counts(c))[0] == 1
    assert (await get(c)).json()["record"] == record


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_trusted_executor_strict_body_three_resources_and_canonical_version(wait_context):
    from opsswarm.identity.tables import OperatorAuthorizationRow

    c = wait_context
    await ready(c)
    for suffix in ("", "/commands"):
        assert (await c.client.get(c.release_base + suffix)).status_code == 401
        assert (
            await c.client.get(c.release_base + suffix, headers=auth_header("alert_source"))
        ).status_code == 403
    assert (await c.client.post(c.release_base, json=c.release_body)).status_code == 401
    for role in ("viewer", "engineer", "approver", "alert_source"):
        assert (await post(c, headers=auth_header(role))).status_code == 403
    for change in (
        {"expected_incident_version": True},
        {"expected_incident_version": "4"},
        {"actor_id": "forged"},
        {"dispatch_eligible": True},
        {"cas_command": {}},
        {"target_version": "stable"},
        {"expires_at": c.clock().isoformat()},
        {"reason": " \n\t"},
        {"approval_content_sha256": "invalid"},
    ):
        assert (await post(c, {**c.release_body, **change})).status_code == 422
    for version in ("0", "9", "01", "1.0", "+1"):
        base = c.release_base.replace("/versions/1/", f"/versions/{version}/")
        assert (await get(c, base=base)).status_code == 422
    absent = c.release_base.replace("/versions/1/", "/versions/2/")
    assert (await post(c, base=absent)).status_code == 404
    for index, change in enumerate(
        (
            {"approval_decision_id": str(uuid4())},
            {"approval_content_sha256": "f" * 64},
            {"expected_incident_version": 99},
        )
    ):
        result = await post(
            c, {**c.release_body, **change, "idempotency_key": f"mismatched-approval-{index}-124"}
        )
        assert result.status_code == 409, result.text
    large = await c.client.post(
        c.release_base,
        content=json.dumps(c.release_body) + " " * 65536,
        headers={**auth_header("executor"), "Content-Type": "application/json"},
    )
    assert large.status_code == 413
    assert c.release_observer.calls == 0
    c.app.state.operator_auth_settings = c.settings.model_copy(update={"mode": "legacy_local"})
    assert (await c.client.post(c.release_base, json=c.release_body)).status_code == 401
    c.app.state.operator_auth_settings = c.settings
    result = await post(c)
    assert result.status_code == 201, result.text
    record = result.json()["request"]["record"]
    async with c.db() as session:
        admission = await session.get(OperatorAuthorizationRow, UUID(record["authorization_id"]))
        assert (
            admission.role == "executor"
            and admission.operation == "prepare_candidate_release_execution"
        )
        assert admission.resources == dict(
            run_id=str(c.started.run.id), job_id=str(c.job_id), version="1"
        )


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_observation_errors_drift_time_and_expiry_do_not_refresh_history(wait_context):
    from opsswarm.execution.release.contracts import ReleaseRequestError

    c = wait_context
    await ready(c)
    observer = c.release_observer
    observer.error = ReleaseRequestError(
        "candidate_release_observation_unavailable", "只读观测不可用"
    )
    assert (await post(c)).status_code == 409
    observer.error = None
    observer.version = "stable"
    assert (
        await post(c, {**c.release_body, "idempotency_key": "already-target-124"})
    ).status_code == 409
    observer.version = "2026.01.01-rc1"
    for offset in (-31, 1):
        observer.offset = offset
        assert (
            await post(c, {**c.release_body, "idempotency_key": f"invalid-time-{offset}-124"})
        ).status_code == 409
    observer.offset = 0
    assert (await counts(c))[0] == 0
    result = await post(c, {**c.release_body, "idempotency_key": "finally-valid-124"})
    assert result.status_code == 201, result.text
    view = result.json()["request"]
    observed_calls = observer.calls
    c.clock.current = datetime.fromisoformat(view["record"]["expires_at"])
    assert (await get(c)).json() == {**view, "availability": "expired"}
    replay = await post(c, {**c.release_body, "idempotency_key": "finally-valid-124"})
    assert replay.status_code == 200 and replay.json()["request"]["availability"] == "expired"
    assert observer.calls == observed_calls
    assert (
        await post(c, {**c.release_body, "idempotency_key": "cannot-renew-124"})
    ).status_code == 409
    assert observer.calls == observed_calls


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_shared_budget_and_revised_verification_block_preparation_without_observation(
    wait_context,
):
    c = wait_context
    await ready(c)
    await approval.occupy_budget(c)
    assert (await post(c)).status_code == 409
    assert c.release_observer.calls == 0
    async with c.db() as session, session.begin():
        await session.execute(text("SET LOCAL session_replication_role=replica"))
        await session.execute(
            text(
                "DELETE FROM investigation_call_reservations WHERE investigation_run_id=:run "
                "AND status='unknown' AND operation='synthesize' "
                "AND invocation_index BETWEEN 40 AND 59"
            ),
            {"run": c.started.run.id},
        )
    result = await post(c, {**c.release_body, "idempotency_key": "budget-restored-124"})
    assert result.status_code == 201, result.text
    await approval.append_verification(c)
    assert (await get(c)).json()["availability"] == "superseded"
    replay = await post(c, {**c.release_body, "idempotency_key": "budget-restored-124"})
    assert replay.status_code == 200 and replay.json()["request"]["availability"] == "superseded"
    assert (
        await post(c, {**c.release_body, "idempotency_key": "cannot-follow-old-basis-124"})
    ).status_code == 409
    assert c.release_observer.calls == 1


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
@pytest.mark.parametrize("phase", ["deadline", "auth"])
async def test_after_flush_deadline_or_expired_credential_rolls_back_all_request_facts(
    wait_context, phase
):
    c = wait_context
    await ready(c)
    before = await counts(c)
    parent, events = await approval.parent_facts(c), await approval.incident_events(c)
    advanced = False

    def advance(session, flush_context):
        nonlocal advanced
        if not advanced and any(
            row.__tablename__ == "candidate_release_execution_commands" and row.outcome == "applied"
            for row in session.new
        ):
            advanced = True
            c.clock.current = (
                c.clock() + timedelta(seconds=30)
                if phase == "deadline"
                else c.clock() + timedelta(days=2)
            )

    event.listen(Session, "after_flush", advance)
    try:
        result = await post(c)
    finally:
        event.remove(Session, "after_flush", advance)
    assert advanced and result.status_code == (409 if phase == "deadline" else 401), result.text
    assert await counts(c) == before
    assert await approval.parent_facts(c) == parent and await approval.incident_events(c) == events


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_damaged_request_or_latest_basis_refuses_reads_replay_and_changed_keys(wait_context):
    c = wait_context
    await ready(c)
    result = await post(c)
    assert result.status_code == 201, result.text
    record = result.json()["request"]["record"]
    await approval.append_verification(c)
    highest = c.verification_record
    before = await counts(c)
    resources = dict(run_id=str(c.started.run.id), job_id=str(c.job_id), version="1")
    for sql, identifier, corrupt, original in (
        (
            "UPDATE candidate_release_execution_requests SET content=CAST(:value AS jsonb) "
            "WHERE id=:id",
            UUID(record["id"]),
            json.dumps({**record["content"], "dispatch_eligible": 0}),
            json.dumps(record["content"]),
        ),
        (
            "UPDATE candidate_condition_verifications SET content_sha256=:value WHERE id=:id",
            UUID(highest["id"]),
            "f" * 64,
            highest["content_sha256"],
        ),
        (
            "UPDATE operator_authorizations SET resources=CAST(:value AS jsonb) WHERE id=:id",
            UUID(record["authorization_id"]),
            json.dumps({**resources, "version": "2"}),
            json.dumps(resources),
        ),
    ):
        try:
            await replace(c, sql, {"id": identifier, "value": corrupt})
            for response in (
                await get(c),
                await get(c, "/commands"),
                await post(c),
                await post(c, {**c.release_body, "idempotency_key": "changed-key-corrupt-124"}),
            ):
                assert response.status_code == 409, response.text
            assert await counts(c) == before
        finally:
            await replace(c, sql, {"id": identifier, "value": original})
    assert (await get(c)).json()["availability"] == "superseded"
    assert c.release_observer.calls == 1
