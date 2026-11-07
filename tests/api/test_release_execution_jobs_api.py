"""真实审批来源、独立执行租约、单次派发及只读恢复的控制面闭环。"""

import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx2
import pytest
from sqlalchemy import event, text
from sqlalchemy.orm import Session

from opsswarm.execution.release_jobs.client import (
    DISPATCH_PATH,
    DispatchClientError,
    DispatchObservation,
    ReleaseDispatchClient,
)
from opsswarm.fault_lab.release.contracts import ReleaseCommandRecord, ReleaseState
from opsswarm.fault_lab.release.dispatch_contracts import DispatchRecord, DispatchView
from opsswarm.fault_lab.release.policy import command_hash
from opsswarm.fault_lab.release.receipts import make_receipt
from tests.api import test_candidate_release_request_api as preparation
from tests.api import test_condition_approval_api as approval
from tests.api import test_human_wait_api as wait_module
from tests.api.test_candidate_conditions_api import replace
from tests.api.test_human_wait_api import wait_context as wait_context
from tests.identity_helpers import auth_header

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture(autouse=True)
def current_fixture_time(monkeypatch, database_session_factory):
    """旧调查夹具继续可控，真实126派发阶段才切换为当前UTC时钟。"""

    del database_session_factory
    base = datetime.now(UTC) - timedelta(seconds=3)
    for name, module in tuple(sys.modules.items()):
        if name.startswith("tests.") and module is not None and hasattr(module, "BASE_TIME"):
            monkeypatch.setattr(module, "BASE_TIME", base)

    class LiveClock:
        def __init__(self, current=None):
            self.current = current if current is not None else base + timedelta(seconds=3)
            self.live = False

        def __call__(self):
            return datetime.now(UTC) if self.live else self.current

    monkeypatch.setattr(wait_module, "TaskClock", LiveClock)


async def ready(c, *, kind="terminal_handoff", real=False):
    from opsswarm.execution.release.store import CandidateReleaseRequestStore
    from opsswarm.execution.release_jobs.store import ReleaseExecutionStore

    await preparation.ready(c, kind=kind)
    # 完整调查/审批夹具可能耗时超过30秒；准备窗口从实际派发附近开始。
    c.clock.current = datetime.now(UTC)
    if real:
        from opsswarm.execution.release.observer import FaultLabReleaseObserver
        from opsswarm.execution.settings import ExecutionSettings
        from opsswarm.fault_lab.app import create_app
        from opsswarm.fault_lab.controller import FaultController
        from opsswarm.fault_lab.release.store import LabReleaseStore

        c.clock.live = True
        c.lab_store = LabReleaseStore(c.db)
        await c.lab_store.initialize()
        await c.lab_store.set_legacy_fault(enabled=True)
        lab_app = create_app(FaultController(), lab_release_store=c.lab_store)
        c.lab_transport = preparation.CountedTransport(lab_app)
        c.dispatch_client = ReleaseDispatchClient(
            ExecutionSettings(fault_lab_base_url="http://order-service"),
            transport=c.lab_transport,
            clock=c.clock,
        )
        c.app.state.candidate_release_request_store = CandidateReleaseRequestStore(
            c.db,
            observer=FaultLabReleaseObserver(transport=c.lab_transport, clock=c.clock),
            clock=c.clock,
        )
    prepared = await preparation.post(c)
    assert prepared.status_code == 201, prepared.text
    c.release_request = prepared.json()["request"]["record"]
    c.execution_base = c.release_base + "/execution-job"
    c.execution_body = dict(
        request_id=c.release_request["id"],
        request_content_sha256=c.release_request["content_sha256"],
        expected_incident_version=c.release_request["incident_version"],
        idempotency_key="candidate-release-execution-127",
        reason="可信执行者显式请求独立任务，保留原准备与审批依据",
    )
    c.app.state.release_execution_store = ReleaseExecutionStore(
        c.db, auth_provider=lambda: c.app.state.operator_auth_settings, clock=c.clock
    )


async def post(c, body=None, *, headers=None, base=None):
    return await c.client.post(
        base or c.execution_base,
        json=c.execution_body if body is None else body,
        headers=headers or auth_header("executor"),
    )


async def get(c, suffix="", *, base=None):
    return await c.client.get((base or c.execution_base) + suffix, headers=auth_header("viewer"))


def job_store(c):
    from opsswarm.execution.release_jobs.job_store import ReleaseExecutionJobStore

    return ReleaseExecutionJobStore(
        c.db, auth_provider=lambda: c.app.state.operator_auth_settings, clock=c.clock
    )


def worker(c, remote, *, worker_id="release-execution-worker"):
    from opsswarm.execution.release_jobs.worker import ReleaseExecutionWorker

    return ReleaseExecutionWorker(job_store(c), client=remote, worker_id=worker_id)


async def counts(c):
    async with c.db() as session:
        return tuple(
            [
                await session.scalar(text(f"SELECT count(*) FROM {table}"))
                for table in (
                    "candidate_release_execution_jobs",
                    "candidate_release_execution_attempts",
                    "candidate_release_execution_results",
                    "candidate_release_execution_job_commands",
                )
            ]
        )


async def parent_snapshot(c):
    from opsswarm.investigation.budget.queries import CallBudgetQueries

    return (
        await approval.parent_facts(c),
        await approval.incident_events(c),
        await CallBudgetQueries(c.db).get_snapshot(c.started.run.id),
        await c.store.get_work_item(c.started.work_item.id),
        await c.store.list_attempts(c.started.run.id),
    )


class FakeDispatchClient:
    """保存同一远端操作；故障后只能从原窗口和原回执构造新观察。"""

    def __init__(self, c):
        self.c, self.calls = c, []
        self.original = self.applied_at = None
        self.command_id = uuid4()
        self.after_register = None
        self.lose_execute_response = False

    def observation(self, command):
        assert self.original == command
        now = self.c.clock()
        applied = self.applied_at is not None
        digest = command_hash(command.command.model_dump(mode="json"))
        record_id = self.command_id if applied else None
        state = ReleaseState(
            service_name="order-service",
            source="local-release-simulation-v1",
            simulation=True,
            version="stable" if applied else "2026.01.01-rc1",
            revision=3 if applied else 2,
            command_count=2 if applied else 1,
            release_500_enabled=not applied,
            updated_at=self.applied_at if applied else command.not_before,
        )
        success = (
            ReleaseCommandRecord(
                id=record_id,
                service_name="order-service",
                sequence=2,
                kind="cas",
                command=command.command,
                command_sha256=digest,
                previous_version="2026.01.01-rc1",
                previous_revision=2,
                version="stable",
                revision=3,
                created_at=self.applied_at,
            )
            if applied
            else None
        )
        record = DispatchRecord(
            **command.model_dump(),
            policy_version="lab-release-dispatch-v1",
            command_sha256=digest,
            status="applied" if applied else "prepared",
            command_id=record_id,
            created_at=command.not_before,
            finished_at=self.applied_at,
        )
        return DispatchObservation(
            view=DispatchView(
                record=record,
                availability=(
                    "applied" if applied else "expired" if now >= command.expires_at else "ready"
                ),
                checked_at=now,
                receipt=make_receipt(command.command.idempotency_key, success, state),
            ),
            observed_at=now,
            source_path=f"{DISPATCH_PATH}/{command.operation_id}",
        )

    async def register(self, command):
        self.calls.append("POST")
        assert self.original is None, "同一远端操作不得再次注册"
        # 网络写出前必须已独立提交phase，另一个会话立即可见。
        visible = (await get(self.c)).json()["record"]
        assert visible["phase"] == "dispatched" and visible["dispatched_at"] is not None
        self.original = command
        if self.after_register is not None:
            await self.after_register()
        return self.observation(command)

    async def execute(self, command):
        self.calls.append("PUT")
        assert self.original == command and self.applied_at is None
        self.applied_at = self.c.clock()
        if self.lose_execute_response:
            raise DispatchClientError("release_dispatch_unavailable", "模拟实际应用后响应丢失")
        return self.observation(command)

    async def lookup(self, command):
        self.calls.append("GET")
        if self.original is None:
            raise DispatchClientError("release_dispatch_unavailable", "原操作不存在，不能重发")
        return self.observation(command)


@pytest.mark.parametrize(
    ("wait_context", "kind"),
    [("candidate_with_change", "terminal_handoff"), ("human_clean_with_change", "human_route")],
    indirect=["wait_context"],
)
async def test_two_sources_queue_exact_request_idempotently_without_parent_mutation(
    wait_context, kind
):
    from opsswarm.identity.tables import OperatorAuthorizationRow

    c = wait_context
    await ready(c, kind=kind)
    before = await parent_snapshot(c)
    pending = await get(c)
    assert pending.status_code == 200, pending.text
    assert pending.json() == dict(record=None, attempts=[], result=None)
    responses = await asyncio.gather(post(c), post(c))
    assert sorted(r.status_code for r in responses) == [200, 201]
    created = next(r for r in responses if r.status_code == 201).json()["job"]
    record = created["record"]
    assert record["status"] == "queued" and record["phase"] == "prepared"
    assert created["attempts"] == [] and created["result"] is None
    assert record["request_id"] == c.release_request["id"]
    assert record["request_content_sha256"] == c.release_request["content_sha256"]
    assert record["operation_id"] == c.release_request["operation_id"]
    assert record["dispatch_command"]["command"] == c.release_request["content"]["cas_command"]
    assert datetime.fromisoformat(
        record["dispatch_command"]["expires_at"]
    ) <= datetime.fromisoformat(c.release_request["expires_at"])
    assert (await post(c)).status_code == 200
    for change in (
        {"reason": "相同幂等键的不同完整命令"},
        {"idempotency_key": "same-request-other-key-127"},
    ):
        assert (await post(c, {**c.execution_body, **change})).status_code == 409
    assert (await get(c)).json() == created
    assert await parent_snapshot(c) == before
    assert c.release_request["content"]["dispatch_eligible"] is False
    async with c.db() as session:
        admission = await session.get(OperatorAuthorizationRow, UUID(record["authorization_id"]))
        assert admission.role == "executor" and "execute_action" in admission.permissions
        assert admission.resources == dict(
            run_id=str(c.started.run.id), job_id=str(c.job_id), version="1"
        )
        assert admission.operation == "create_candidate_release_execution_job"
    page = (await get(c, "/commands?limit=1")).json()
    rest = (await get(c, f"/commands?after_cursor={page['next_cursor']}")).json()
    assert [item["outcome"] for item in page["items"] + rest["items"]][:3] == [
        "applied",
        "replayed",
        "replayed",
    ]
    errors = [item for item in page["items"] + rest["items"] if item["outcome"] == "error"]
    assert {item["error_code"] for item in errors} == {
        "release_execution_idempotency_conflict",
        "release_execution_job_exists",
    }
    assert "raw_body" not in json.dumps(page)
    assert (await counts(c))[:3] == (1, 0, 0)
    errors = [item for item in page["items"] + rest["items"] if item["outcome"] == "error"]
    assert len(errors) == 2
    assert {item["error_code"] for item in errors} == {
        "release_execution_idempotency_conflict",
        "release_execution_job_exists",
    }
    assert all(item["http_status"] == 409 and item["execution_job_id"] is None for item in errors)


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_executor_strict_body_three_resources_canonical_version_and_original_deadline(
    wait_context,
):
    c = wait_context
    await ready(c)
    assert (await c.client.post(c.execution_base, json=c.execution_body)).status_code == 401
    for suffix in ("", "/commands"):
        assert (await c.client.get(c.execution_base + suffix)).status_code == 401
        assert (
            await c.client.get(c.execution_base + suffix, headers=auth_header("alert_source"))
        ).status_code == 403
    for role in ("viewer", "engineer", "approver", "alert_source"):
        assert (await post(c, headers=auth_header(role))).status_code == 403
    for change in (
        {"expected_incident_version": True},
        {"expected_incident_version": "4"},
        {"actor_id": "forged-executor"},
        {"dispatch_command": {}},
        {"operation_id": str(uuid4())},
        {"expires_at": c.clock().isoformat()},
        {"request_content_sha256": "invalid"},
        {"reason": " \n\t"},
    ):
        assert (await post(c, {**c.execution_body, **change})).status_code == 422
    for version in ("0", "9", "01", "1.0", "+1"):
        base = c.execution_base.replace("/versions/1/", f"/versions/{version}/")
        assert (await get(c, base=base)).status_code == 422
    for index, change in enumerate(
        (
            {"request_id": str(uuid4())},
            {"request_content_sha256": "f" * 64},
            {"expected_incident_version": 99},
        )
    ):
        assert (
            await post(
                c, {**c.execution_body, **change, "idempotency_key": f"wrong-request-{index}-127"}
            )
        ).status_code == 409
    for original, replacement in (
        (str(c.started.run.id), str(uuid4())),
        (str(c.job_id), str(uuid4())),
        ("/versions/1/", "/versions/2/"),
    ):
        result = await post(c, base=c.execution_base.replace(original, replacement))
        assert result.status_code == 404, result.text
    large = await c.client.post(
        c.execution_base,
        content=json.dumps(c.execution_body) + " " * 65536,
        headers={**auth_header("executor"), "Content-Type": "application/json"},
    )
    assert large.status_code == 413
    c.app.state.operator_auth_settings = c.settings.model_copy(update={"mode": "legacy_local"})
    assert (await c.client.post(c.execution_base, json=c.execution_body)).status_code == 401
    c.app.state.operator_auth_settings = c.settings
    c.clock.current = datetime.fromisoformat(c.release_request["expires_at"])
    assert (await post(c)).status_code == 409
    assert (await counts(c))[:3] == (0, 0, 0)


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_status_snapshot_allows_claim_and_keeps_one_committed_view(wait_context, monkeypatch):
    from opsswarm.execution.release_jobs import store as release_store

    c = wait_context
    await ready(c)
    c.clock.live = True
    assert (await post(c)).status_code == 201
    entered, resume = asyncio.Event(), asyncio.Event()
    original_load = release_store.load_request

    async def paused_load(*args, **kwargs):
        entered.set()
        await resume.wait()
        return await original_load(*args, **kwargs)

    monkeypatch.setattr(release_store, "load_request", paused_load)
    reading = asyncio.create_task(get(c))
    try:
        await asyncio.wait_for(entered.wait(), timeout=30)
        # 读取停在来源重建阶段时，Worker 仍须取得父锁并提交领取。
        claim = await job_store(c).claim_next(worker_id="claim-during-status-read")
        assert claim is not None
    finally:
        resume.set()
        response = await reading
    assert response.status_code == 200, response.text
    snapshot = response.json()
    assert snapshot["record"]["status"] == "queued"
    assert snapshot["attempts"] == [] and snapshot["result"] is None
    latest = (await get(c)).json()
    assert latest["record"]["status"] == "running"
    assert len(latest["attempts"]) == 1
    assert latest["attempts"][0]["id"] == str(claim.attempt_id)


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_two_workers_claim_single_attempt_and_preserve_original_parent_facts(wait_context):
    c = wait_context
    await ready(c)
    assert (await post(c)).status_code == 201
    before = await parent_snapshot(c)
    remote = FakeDispatchClient(c)
    results = await asyncio.gather(
        worker(c, remote, worker_id="release-one").run_once(),
        worker(c, remote, worker_id="release-two").run_once(),
    )
    assert sorted(results) == [False, True]
    view = (await get(c)).json()
    assert view["record"]["status"] == "succeeded", (view["result"], remote.calls)
    assert len(view["attempts"]) == 1 and view["attempts"][0]["status"] == "succeeded"
    assert (
        view["result"]["outcome"] == "applied_current"
        and view["result"]["parent_advanced"] is False
    )
    assert remote.calls.count("POST") == remote.calls.count("PUT") == 1
    assert await parent_snapshot(c) == before
    assert not await worker(c, remote).run_once()


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
@pytest.mark.parametrize("lost_response", [False, True])
async def test_actual_126_asgi_cas_and_lost_response_only_query_original_operation(
    wait_context, lost_response
):
    c = wait_context
    await ready(c, real=True)
    assert (await post(c)).status_code == 201
    before = await parent_snapshot(c)
    original_transport = c.lab_transport

    class Lost(httpx2.AsyncBaseTransport):
        async def handle_async_request(self, request):
            response = await original_transport.handle_async_request(request)
            if request.method == "PUT" and request.url.path.startswith(DISPATCH_PATH):
                assert response.status_code == 201
                await response.aclose()
                raise httpx2.ReadError("实际CAS完成后响应丢失", request=request)
            return response

    if lost_response:
        c.dispatch_client = ReleaseDispatchClient(transport=Lost(), clock=c.clock)
    assert await worker(c, c.dispatch_client).run_once()
    view = (await get(c)).json()
    assert view["record"]["status"] == "succeeded", (view["result"], c.lab_transport.calls)
    assert view["result"]["outcome"] == "applied_current"
    assert (
        view["result"]["observation"]["view"]["receipt"]["command"]["command"]
        == c.release_request["content"]["cas_command"]
    )
    calls = original_transport.calls
    path = DISPATCH_PATH + "/" + c.release_request["operation_id"]
    assert calls.count(("POST", DISPATCH_PATH)) == calls.count(("PUT", path)) == 1
    if lost_response:
        assert calls.count(("GET", path)) >= 1
    assert (await c.lab_store.get()).version == "stable"
    async with c.db() as session:
        assert await session.scalar(text("SELECT count(*) FROM lab_release_commands")) == 2
        assert await session.scalar(text("SELECT count(*) FROM lab_release_dispatches")) == 1
    assert await parent_snapshot(c) == before
    assert (await preparation.get(c)).json()["record"]["content"]["dispatch_eligible"] is False


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_actual_126_aba_drift_rejects_original_cas_without_claiming_success(wait_context):
    c = wait_context
    await ready(c, real=True)
    queued = await post(c)
    assert queued.status_code == 201, queued.text
    before = await parent_snapshot(c)
    started = datetime.now(UTC)

    class DriftAfterRegistration:
        async def register(self, command):
            observation = await c.dispatch_client.register(command)
            # 版本回到rc1仍不等于原CAS前态，修订必须保留ABA变化。
            await c.lab_store.set_legacy_fault(enabled=False)
            await c.lab_store.set_legacy_fault(enabled=True)
            return observation

        async def execute(self, command):
            return await c.dispatch_client.execute(command)

        async def lookup(self, command):
            return await c.dispatch_client.lookup(command)

    assert await worker(c, DriftAfterRegistration()).run_once()
    view = (await get(c)).json()
    elapsed = (datetime.now(UTC) - started).total_seconds()
    assert view["record"]["status"] != "succeeded", (view["result"], elapsed)
    assert view["result"]["outcome"] in {"unknown", "not_applied"}, (view["result"], elapsed)
    observation = view["result"]["observation"]
    assert observation is not None, (view["result"], elapsed)
    assert observation["view"]["record"]["status"] == "prepared"
    assert observation["view"]["receipt"]["command"] is None
    assert observation["view"]["receipt"]["current"]["revision"] == 4
    if observation["view"]["availability"] == "ready":
        assert view["result"]["outcome"] == "unknown"
    else:
        assert observation["view"]["availability"] == "expired"
        assert view["result"]["outcome"] == "not_applied"
    path = DISPATCH_PATH + "/" + c.release_request["operation_id"]
    calls = c.lab_transport.calls
    assert calls.count(("POST", DISPATCH_PATH)) == calls.count(("PUT", path)) == 1
    assert calls.count(("GET", path)) >= 1
    lab = await c.lab_store.get()
    assert lab.version == "2026.01.01-rc1" and lab.revision == 4
    async with c.db() as session:
        assert await session.scalar(text("SELECT count(*) FROM lab_release_commands")) == 3
        assert await session.scalar(text("SELECT count(*) FROM lab_release_dispatches")) == 1
    assert await parent_snapshot(c) == before


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
@pytest.mark.parametrize("change", ["credential", "verification", "budget"])
async def test_after_registration_revocation_or_changed_basis_never_puts_and_read_only_converges(
    wait_context, change
):
    c = wait_context
    await ready(c)
    assert (await post(c)).status_code == 201
    remote = FakeDispatchClient(c)

    async def invalidate():
        if change == "credential":
            c.app.state.operator_auth_settings = c.settings.model_copy(
                update={
                    "credentials": tuple(
                        i for i in c.settings.credentials if i.credential_id != "executor"
                    )
                }
            )
        elif change == "verification":
            await approval.append_verification(c)
        else:
            await approval.occupy_budget(c)

    remote.after_register = invalidate
    assert await worker(c, remote).run_once()
    view = (await get(c)).json()
    assert "PUT" not in remote.calls and remote.calls.count("POST") == 1
    assert "GET" in remote.calls
    assert view["record"]["phase"] == "dispatched"
    assert view["result"]["outcome"] == "unknown"
    assert view["result"]["observation"]["view"]["record"]["status"] == "prepared"


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_crash_after_phase_commit_recovers_only_get_and_rejects_old_lease_result(
    wait_context,
):
    from opsswarm.execution.release_jobs.contracts import ExecutionJobError

    c = wait_context
    await ready(c)
    assert (await post(c)).status_code == 201
    before = await parent_snapshot(c)
    jobs = job_store(c)
    first = await jobs.claim_next(worker_id="crashed-release-worker")
    assert first is not None
    dispatch, recovery = await jobs.get_dispatch(first)
    assert recovery is False
    await jobs.begin_dispatch(first)
    # 模拟原尝试在远端完成CAS后、本地保存之前崩溃。
    remote = FakeDispatchClient(c)
    remote.original, remote.applied_at = dispatch, c.clock()
    stale_observation = remote.observation(dispatch)
    c.clock.current = first.lease_expires_at + timedelta(microseconds=1)
    assert await worker(c, remote, worker_id="recovery-only-reader").run_once()
    assert remote.calls == ["GET"]
    view = (await get(c)).json()
    assert view["record"]["status"] == "succeeded" and view["record"]["attempt_count"] == 2
    assert [a["status"] for a in view["attempts"]] == ["lease_expired", "succeeded"]
    assert view["result"]["outcome"] == "applied_current"
    with pytest.raises(ExecutionJobError):
        await jobs.complete(first, stale_observation, reason_code="late-original-result")
    assert (await get(c)).json() == view
    assert await parent_snapshot(c) == before


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
@pytest.mark.parametrize("stage", ["claimed", "marked", "registered"])
async def test_remaining_crash_boundaries_never_redispatch_or_accept_old_lease(wait_context, stage):
    from opsswarm.execution.release_jobs.contracts import ExecutionJobError

    c = wait_context
    await ready(c)
    created = await post(c)
    assert created.status_code == 201, created.text
    before = await parent_snapshot(c)
    jobs = job_store(c)
    first = await jobs.claim_next(worker_id=f"crashed-at-{stage}")
    assert first is not None
    dispatch, recovery = await jobs.get_dispatch(first)
    assert recovery is False
    remote = FakeDispatchClient(c)
    if stage in {"marked", "registered"}:
        await jobs.begin_dispatch(first)
    if stage == "registered":
        await remote.register(dispatch)
        assert remote.calls == ["POST"]
    original_calls = list(remote.calls)
    c.clock.current = first.lease_expires_at + timedelta(microseconds=1)
    assert c.clock() > dispatch.expires_at
    assert await worker(c, remote, worker_id=f"recover-{stage}-read-only").run_once()
    view = (await get(c)).json()
    assert view["record"]["attempt_count"] == 2
    assert [a["status"] for a in view["attempts"]] == ["lease_expired", "failed"]
    recovery_calls = remote.calls[len(original_calls) :]
    if stage == "claimed":
        assert remote.calls == []
        assert view["record"]["phase"] == "prepared"
        assert view["record"]["status"] == "failed"
        assert view["result"]["outcome"] == "blocked"
        assert view["result"]["observation"] is None
    elif stage == "marked":
        assert recovery_calls and set(recovery_calls) == {"GET"}
        assert view["record"]["phase"] == "dispatched"
        assert view["record"]["status"] == "unknown"
        assert view["result"]["outcome"] == "unknown"
        assert view["result"]["observation"] is None
    else:
        assert recovery_calls == ["GET"]
        assert remote.calls.count("POST") == 1 and "PUT" not in remote.calls
        assert view["record"]["phase"] == "dispatched"
        assert view["record"]["status"] == "failed"
        assert view["result"]["outcome"] == "not_applied"
        observation = view["result"]["observation"]
        assert observation["view"]["record"]["status"] == "prepared"
        assert observation["view"]["availability"] == "expired"
        assert observation["view"]["receipt"]["command"] is None
    assert view["result"]["parent_advanced"] is False
    with pytest.raises(ExecutionJobError) as late:
        await jobs.complete(first, reason_code="late-crashed-original-attempt")
    assert late.value.code == "release_execution_lease_lost"
    assert (await get(c)).json() == view
    assert await parent_snapshot(c) == before


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_three_expired_claims_terminalize_unknown_without_fourth_attempt_or_redispatch(
    wait_context,
):
    c = wait_context
    await ready(c)
    assert (await post(c)).status_code == 201
    before = await parent_snapshot(c)
    jobs = job_store(c)
    first = await jobs.claim_next(worker_id="release-crash-one")
    assert first is not None
    await jobs.begin_dispatch(first)
    c.clock.current = first.lease_expires_at + timedelta(microseconds=1)
    second = await jobs.claim_next(worker_id="release-crash-two")
    assert second is not None and second.number == 2
    _, recovery = await jobs.get_dispatch(second)
    assert recovery is True
    c.clock.current = second.lease_expires_at + timedelta(microseconds=1)
    third = await jobs.claim_next(worker_id="release-crash-three")
    assert third is not None and third.number == 3
    c.clock.current = third.lease_expires_at + timedelta(microseconds=1)
    assert await jobs.claim_next(worker_id="must-not-create-fourth") is None
    view = (await get(c)).json()
    assert view["record"]["status"] == "unknown" and view["record"]["attempt_count"] == 3
    assert [a["status"] for a in view["attempts"]] == ["lease_expired"] * 3
    assert view["result"]["outcome"] == "unknown" and view["result"]["observation"] is None
    assert await parent_snapshot(c) == before


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_corrupt_job_or_authorization_refuses_read_replay_changed_key_and_claim(wait_context):
    from opsswarm.execution.release_jobs.contracts import ExecutionJobError

    c = wait_context
    await ready(c)
    created = await post(c)
    assert created.status_code == 201, created.text
    record = created.json()["job"]["record"]
    before = await counts(c)
    for sql, identifier, corrupt, original in (
        (
            "UPDATE candidate_release_execution_jobs SET dispatch_sha256=:value WHERE id=:id",
            UUID(record["id"]),
            "f" * 64,
            record["dispatch_sha256"],
        ),
        (
            "UPDATE operator_authorizations SET resources=CAST(:value AS jsonb) WHERE id=:id",
            UUID(record["authorization_id"]),
            json.dumps(dict(run_id=str(c.started.run.id), job_id=str(c.job_id), version="2")),
            json.dumps(dict(run_id=str(c.started.run.id), job_id=str(c.job_id), version="1")),
        ),
    ):
        try:
            await replace(c, sql, {"id": identifier, "value": corrupt})
            for response in (
                await get(c),
                await get(c, "/commands"),
                await post(c),
                await post(c, {**c.execution_body, "idempotency_key": "changed-key-corrupt-127"}),
            ):
                assert response.status_code == 409, response.text
            with pytest.raises(ExecutionJobError):
                await job_store(c).claim_next(worker_id="reject-damaged-job")
            assert await counts(c) == before
            async with c.db() as session:
                assert (
                    await session.scalar(
                        text(
                            "SELECT count(*) FROM candidate_release_execution_job_commands "
                            "WHERE outcome='error'"
                        )
                    )
                    == 0
                )
        finally:
            await replace(c, sql, {"id": identifier, "value": original})


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_after_flush_expiry_rolls_back_job_and_business_success_audit(wait_context):
    c = wait_context
    await ready(c)
    before = await counts(c)
    advanced = False

    def expire_after_flush(session, flush_context):
        nonlocal advanced
        if not advanced and any(
            row.__tablename__ == "candidate_release_execution_job_commands"
            and row.outcome == "applied"
            for row in session.new
        ):
            advanced = True
            c.clock.current = datetime.fromisoformat(c.release_request["expires_at"])

    event.listen(Session, "after_flush", expire_after_flush)
    try:
        response = await post(c)
    finally:
        event.remove(Session, "after_flush", expire_after_flush)
    assert advanced and response.status_code == 409, response.text
    assert await counts(c) == before
