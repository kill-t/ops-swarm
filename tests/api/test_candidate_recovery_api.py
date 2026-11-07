"""真实 PostgreSQL 下的独立恢复采样、租约接管与只读控制面隔离。"""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx2
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from opsswarm.execution.release_jobs.contracts import ExecutionJobError
from opsswarm.investigation.budget.contracts import CallBudgetPolicy
from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.verification.candidate.contracts import (
    PROBE_ORDER_ID,
    PROBE_PATH,
    ProbeObservation,
    RecoveryError,
)
from opsswarm.verification.candidate.probe import Probe
from opsswarm.verification.candidate.store import CandidateRecoveryStore
from opsswarm.verification.candidate.worker import CandidateRecoveryWorker
from tests.api.test_candidate_conditions_api import replace
from tests.api.test_release_execution_jobs_api import (
    FakeDispatchClient,
    parent_snapshot,
    post,
    ready,
    worker,
)
from tests.api.test_release_execution_jobs_api import (
    current_fixture_time as current_fixture_time,
)
from tests.api.test_release_execution_jobs_api import (
    get as execution_get,
)
from tests.api.test_release_execution_jobs_api import (
    wait_context as wait_context,
)
from tests.identity_helpers import auth_header

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def prepare(c, *, real=False, execute=True):
    await ready(c, real=real)
    c.clock.live = True
    c.recovery = CandidateRecoveryStore(c.db, clock=c.clock)
    c.app.state.candidate_recovery_store = c.recovery
    c.recovery_base = c.execution_base + "/recovery-verification"
    response = await post(c)
    assert response.status_code == 201, response.text
    if execute:
        remote = c.dispatch_client if real else FakeDispatchClient(c)
        assert await worker(c, remote).run_once()
        result = (await execution_get(c)).json()
        assert result["result"]["outcome"] == "applied_current", (
            result["result"],
            c.lab_transport.calls if real else None,
        )
    return (await execution_get(c)).json()


async def get(c, *, base=None):
    return await c.client.get(base or c.recovery_base, headers=auth_header("viewer"))


def observation(c):
    return ProbeObservation(
        requested_at=c.clock(),
        observed_at=c.clock(),
        http_status=200,
        value=0.0,
        order_id=PROBE_ORDER_ID,
    )


async def advance(c, seconds=2):
    await asyncio.sleep(seconds)


async def expire_lease(c, claim):
    # PostgreSQL 用数据库时钟禁止提前接管，测试必须真正等待原30秒租约。
    remaining = (claim.lease_expires_at - datetime.now(UTC)).total_seconds()
    if remaining > 0:
        await asyncio.sleep(remaining + 0.05)
    c.clock.current = claim.lease_expires_at + timedelta(microseconds=1)


def recovery_worker(c, transport):
    return CandidateRecoveryWorker(
        c.recovery,
        probe=Probe(transport=transport, clock=c.clock),
        worker_id="candidate-recovery-test",
        sleep=asyncio.sleep,
    )


async def counts(c):
    async with c.db() as session:
        return tuple(
            [
                await session.scalar(text(f"SELECT count(*) FROM {table}"))
                for table in (
                    "candidate_recovery_jobs",
                    "candidate_recovery_attempts",
                    "candidate_recovery_samples",
                    "candidate_recovery_results",
                )
            ]
        )


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
@pytest.mark.parametrize("pool_failure", [False, True])
async def test_actual_cas_business_orders_determine_recovery_without_parent_mutation(
    wait_context,
    pool_failure,
    monkeypatch,
):
    c = wait_context
    execution = await prepare(c, real=True)
    before = await parent_snapshot(c)
    empty = await get(c)
    assert empty.status_code == 200, empty.text
    assert empty.json() == dict(record=None, attempts=[], samples=[], result=None)
    assert await counts(c) == (0, 0, 0, 0)
    original_calls = list(c.lab_transport.calls)
    seen = []
    early_claims = []
    authorize = c.recovery.authorize_sample

    async def authorize_after_wait(claim):
        sequence = await authorize(claim)
        if not pool_failure and sequence == 2 and not early_claims:
            # 在真实租约核验后模拟一次尚需等待，Worker 必须重新准入后才可探测。
            early_claims.append(claim)
            raise RecoveryError("recovery_sample_too_early", "恢复采样间隔不足")
        return sequence

    monkeypatch.setattr(c.recovery, "authorize_sample", authorize_after_wait)

    class BusinessTransport(httpx2.AsyncBaseTransport):
        async def handle_async_request(self, request):
            assert request.method == "GET" and request.url.path == PROBE_PATH
            if pool_failure and len(seen) == 1:
                async with httpx2.AsyncClient(
                    transport=c.lab_transport,
                    base_url="http://order-service",
                ) as client:
                    changed = await client.put(
                        "/__admin/configuration/database-pool",
                        json=dict(value=1, expected_value=50),
                    )
                assert changed.status_code == 200, changed.text
            seen.append(c.clock())
            # 下一次网络请求前，前一份样本必须已在独立事务可见。
            assert (await counts(c))[2] == len(seen) - 1
            return await c.lab_transport.handle_async_request(request)

    runner = recovery_worker(c, BusinessTransport())
    assert await runner.run_once()
    view = (await get(c)).json()
    assert view["record"]["id"] == execution["record"]["id"]
    assert view["record"]["operation_id"] == execution["result"]["operation_id"]
    assert view["record"]["policy_version"] == "candidate-recovery-v1"
    assert view["record"]["status"] == "completed"
    assert view["result"]["outcome"] == ("not_recovered" if pool_failure else "recovered")
    assert view["result"]["parent_advanced"] is False
    assert len(view["attempts"]) == 1 and len(view["samples"]) == 3
    assert len(early_claims) == (0 if pool_failure else 1)
    if early_claims:
        assert str(early_claims[0].attempt_id) == view["attempts"][0]["id"]
    assert [s["observation"]["http_status"] for s in view["samples"]] == (
        [200, 500, 500] if pool_failure else [200, 200, 200]
    )
    assert all(s["observation"]["path"] == PROBE_PATH for s in view["samples"])
    assert all((b - a).total_seconds() >= 2 for a, b in zip(seen, seen[1:], strict=False))
    assert await counts(c) == (1, 1, 3, 1)
    assert not await runner.run_once()
    assert len(seen) == 3
    state = await c.lab_store.get()
    assert state.version == "stable" and state.release_500_enabled is False
    assert (await execution_get(c)).json() == execution
    assert await parent_snapshot(c) == before
    assert c.lab_transport.calls[: len(original_calls)] == original_calls


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
@pytest.mark.parametrize("unknown", [False, True])
async def test_source_failure_and_unknown_execution_never_manufacture_recovery(
    wait_context,
    unknown,
):
    c = wait_context
    await prepare(c, execute=not unknown)
    if unknown:
        remote = FakeDispatchClient(c)

        async def revoke_executor():
            c.app.state.operator_auth_settings = c.settings.model_copy(
                update={
                    "credentials": tuple(
                        item for item in c.settings.credentials if item.credential_id != "executor"
                    ),
                }
            )

        remote.after_register = revoke_executor
        assert await worker(c, remote).run_once()
        c.app.state.operator_auth_settings = c.settings
        assert (await execution_get(c)).json()["result"]["outcome"] == "unknown"
    execution = (await execution_get(c)).json()
    before = await parent_snapshot(c)
    calls = []

    async def unavailable(request):
        calls.append((request.method, request.url.path))
        raise httpx2.ConnectError("隔离来源暂时不可用", request=request)

    runner = recovery_worker(c, httpx2.MockTransport(unavailable))
    assert await runner.run_once() is (not unknown)
    view = (await get(c)).json()
    assert view["result"]["outcome"] == ("execution_unknown" if unknown else "source_failed")
    assert view["result"]["parent_advanced"] is False
    assert len(view["attempts"]) == len(view["samples"]) == (0 if unknown else 1)
    assert calls == ([] if unknown else [("GET", PROBE_PATH)])
    if not unknown:
        assert view["samples"][0]["observation"] is None
        assert view["samples"][0]["error_code"] == "probe_unavailable"
        assert "隔离来源暂时不可用" not in json.dumps(view, ensure_ascii=False)
    assert not await runner.run_once()
    assert (await execution_get(c)).json() == execution
    assert await parent_snapshot(c) == before


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_single_claim_persisted_samples_and_crash_retry_do_not_join_attempts(wait_context):
    c = wait_context
    execution = await prepare(c)
    before = await parent_snapshot(c)
    claims = await asyncio.gather(
        c.recovery.claim_next(worker_id="recovery-one"),
        c.recovery.claim_next(worker_id="recovery-two"),
    )
    assert sum(claim is not None for claim in claims) == 1
    first = next(claim for claim in claims if claim is not None)
    view = (await get(c)).json()
    window_start, window_end = view["record"]["window_start"], view["record"]["window_end"]
    assert window_start == execution["record"]["completed_at"]
    async with c.db() as session:
        run = await session.get(InvestigationRunRow, c.started.run.id)
        assert run is not None
        budget = CallBudgetPolicy.model_validate(run.call_budget)
        expected_end = min(
            datetime.fromisoformat(window_start) + timedelta(seconds=120),
            run.started_at + timedelta(seconds=budget.max_duration_seconds),
        )
    assert datetime.fromisoformat(window_end) == expected_end
    for sequence in (1, 2):
        if sequence == 2:
            await advance(c)
        assert await c.recovery.authorize_sample(first) == sequence
        saved = await c.recovery.save_sample(first, sequence, observation=observation(c))
        assert saved.sequence == sequence
        assert (await counts(c))[2] == sequence
    with pytest.raises(RecoveryError, match="序号"):
        await c.recovery.save_sample(first, 2, observation=observation(c))
    with pytest.raises(RecoveryError) as early:
        await c.recovery.authorize_sample(first)
    assert early.value.code == "recovery_sample_too_early"
    with pytest.raises(RecoveryError) as incomplete:
        await c.recovery.complete(first)
    assert incomplete.value.code == "recovery_samples_incomplete"
    await expire_lease(c, first)
    second = await c.recovery.claim_next(worker_id="recovery-after-crash")
    assert second is not None and second.number == 2 and second.attempt_id != first.attempt_id
    assert second.lease_token != first.lease_token
    for operation in (
        c.recovery.authorize_sample(first),
        c.recovery.save_sample(first, 3, observation=observation(c)),
        c.recovery.complete(first),
    ):
        with pytest.raises(RecoveryError) as stale:
            await operation
        assert stale.value.code == "recovery_lease_lost"
    assert await c.recovery.authorize_sample(second) == 1
    for sequence in (1, 2, 3):
        if sequence > 1:
            await advance(c)
        await c.recovery.save_sample(second, sequence, observation=observation(c))
        if sequence < 3:
            with pytest.raises(RecoveryError) as unfinished:
                await c.recovery.complete(second)
            assert unfinished.value.code == "recovery_samples_incomplete"
    await c.recovery.complete(second)
    view = (await get(c)).json()
    assert [a["status"] for a in view["attempts"]] == ["lease_expired", "succeeded"]
    assert len(view["samples"]) == 5 and view["result"]["outcome"] == "recovered"
    assert view["result"]["attempt_id"] == str(second.attempt_id)
    assert view["record"]["window_start"] == window_start
    assert view["record"]["window_end"] == window_end
    # 正常数据库连接不能改写样本、终态或固定窗口；损坏测试才显式关闭守卫。
    for sql in (
        "UPDATE candidate_recovery_samples SET created_at=created_at+interval '1 second'",
        "DELETE FROM candidate_recovery_samples",
        "UPDATE candidate_recovery_results SET content_sha256=repeat('f',64)",
        "DELETE FROM candidate_recovery_results",
        "UPDATE candidate_recovery_jobs SET window_end=window_end+interval '1 second'",
        "UPDATE candidate_recovery_jobs SET updated_at=updated_at+interval '1 second'",
    ):
        with pytest.raises(DBAPIError):
            async with c.db() as session, session.begin():
                await session.execute(text(sql))
    assert (await get(c)).json() == view
    assert await counts(c) == (1, 2, 5, 1)
    assert (await execution_get(c)).json() == execution
    assert await parent_snapshot(c) == before


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
@pytest.mark.parametrize("delayed", [False, True])
async def test_three_crashes_or_original_window_expiry_terminalize_without_network(
    wait_context,
    delayed,
):
    c = wait_context
    execution = await prepare(c)
    before = await parent_snapshot(c)
    if delayed:
        deadline = datetime.fromisoformat(execution["record"]["completed_at"]) + timedelta(
            seconds=120,
        )
        while (remaining := (deadline - datetime.now(UTC)).total_seconds()) > 0:
            await asyncio.sleep(min(30, remaining + 0.05))
    else:
        for number in (1, 2, 3):
            claim = await c.recovery.claim_next(worker_id=f"crash-{number}")
            assert claim is not None and claim.number == number
            await expire_lease(c, claim)
    calls = []

    async def forbidden(request):
        calls.append(request.url.path)
        raise AssertionError("窗口或尝试耗尽后不得探测")

    assert not await recovery_worker(c, httpx2.MockTransport(forbidden)).run_once()
    view = (await get(c)).json()
    assert view["record"]["status"] == "completed"
    assert view["result"]["outcome"] == "source_failed"
    assert view["result"]["reason_code"] == "recovery_exhausted"
    assert view["result"]["parent_advanced"] is False
    assert len(view["attempts"]) == (0 if delayed else 3)
    assert all(a["status"] == "lease_expired" for a in view["attempts"])
    assert view["samples"] == [] and calls == []
    assert await c.recovery.claim_next(worker_id="never-fourth") is None
    assert (await get(c)).json() == view
    assert await parent_snapshot(c) == before


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_read_permissions_versions_and_corrupt_history_fail_closed(wait_context):
    c = wait_context
    execution = await prepare(c)
    assert (await c.client.get(c.recovery_base)).status_code == 401
    assert (
        await c.client.get(c.recovery_base, headers=auth_header("alert_source"))
    ).status_code == 403
    c.app.state.operator_auth_settings = c.settings.model_copy(update={"mode": "legacy_local"})
    assert (await c.client.get(c.recovery_base)).status_code == 401
    c.app.state.operator_auth_settings = c.settings
    for version in ("0", "01", "+1", "1.0", "9"):
        response = await get(
            c,
            base=c.recovery_base.replace("/versions/1/", f"/versions/{version}/"),
        )
        assert response.status_code == 422, response.text
    assert (await get(c)).headers["cache-control"] == "no-store"
    assert await counts(c) == (0, 0, 0, 0)
    async with c.db() as session:
        approval_id, approval_hash = (
            await session.execute(
                text(
                    "SELECT approval.id,approval.content_sha256 "
                    "FROM condition_approval_decisions approval "
                    "JOIN candidate_release_execution_requests request "
                    "ON request.approval_decision_id=approval.id WHERE request.id=:id"
                ),
                {"id": UUID(execution["record"]["request_id"])},
            )
        ).one()
    approval_sql = "UPDATE condition_approval_decisions SET content_sha256=:value WHERE id=:id"
    try:
        await replace(c, approval_sql, {"id": approval_id, "value": "f" * 64})
        assert (await get(c)).status_code == 409
        with pytest.raises((RecoveryError, ExecutionJobError)):
            await c.recovery.claim_next(worker_id="reject-corrupt-upstream-approval")
        assert await counts(c) == (0, 0, 0, 0)
    finally:
        await replace(c, approval_sql, {"id": approval_id, "value": approval_hash})
    claim = await c.recovery.claim_next(worker_id="integrity-reader")
    assert claim is not None
    first = await c.recovery.save_sample(claim, 1, observation=observation(c))
    before = await counts(c)
    async with c.db() as session:
        original_hash = await session.scalar(
            text(
                "SELECT content_sha256 FROM candidate_release_execution_results WHERE job_id=:id",
            ),
            {"id": UUID(execution["record"]["id"])},
        )
    for sql, parameters, original, corrupt in (
        (
            "UPDATE candidate_recovery_samples SET observation=CAST(:value AS jsonb) "
            "WHERE attempt_id=:id AND sequence=1",
            {"id": claim.attempt_id},
            json.dumps(first.observation.model_dump(mode="json")),
            json.dumps({**first.observation.model_dump(mode="json"), "path": "/health"}),
        ),
        (
            "UPDATE candidate_release_execution_results SET content_sha256=:value WHERE job_id=:id",
            {"id": claim.job_id},
            original_hash,
            "f" * 64,
        ),
    ):
        try:
            await replace(c, sql, {**parameters, "value": corrupt})
            response = await get(c)
            assert response.status_code == 409, response.text
            for operation in (
                c.recovery.authorize_sample(claim),
                c.recovery.save_sample(claim, 2, observation=observation(c)),
                c.recovery.complete(claim),
            ):
                with pytest.raises((RecoveryError, ExecutionJobError)):
                    await operation
            current = c.clock.current
            try:
                c.clock.live = False
                c.clock.current = claim.lease_expires_at + timedelta(microseconds=1)
                with pytest.raises((RecoveryError, ExecutionJobError)):
                    await c.recovery.claim_next(worker_id="reject-corrupt-history")
            finally:
                c.clock.current = current
                c.clock.live = True
            assert await counts(c) == before
        finally:
            await replace(c, sql, {**parameters, "value": original})
    for sequence in (2, 3):
        await advance(c)
        await c.recovery.save_sample(claim, sequence, observation=observation(c))
    await c.recovery.complete(claim)
    view = (await get(c)).json()
    assert view["result"]["outcome"] == "recovered"
    sql = "UPDATE candidate_recovery_results SET content_sha256=:value WHERE job_id=:id"
    async with c.db() as session:
        digest = await session.scalar(
            text(
                "SELECT content_sha256 FROM candidate_recovery_results WHERE job_id=:id",
            ),
            {"id": claim.job_id},
        )
    try:
        await replace(c, sql, {"id": claim.job_id, "value": "f" * 64})
        response = await get(c)
        assert response.status_code == 409, response.text
        assert await counts(c) == (1, 1, 3, 1)
    finally:
        await replace(c, sql, {"id": claim.job_id, "value": digest})
    assert (await get(c)).json() == view
