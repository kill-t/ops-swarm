"""真实来源收尾、旧历史可读与父事实原子隔离。"""

import asyncio
from datetime import datetime
from uuid import UUID, uuid4

import httpx2
import pytest
from opsswarm.investigation.settlement.store import CandidateSettlementStore
from sqlalchemy import event, text
from sqlalchemy.orm import Session

from opsswarm.fault_lab.release.receipts import make_receipt
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.settlement.contracts import SettlementContent
from opsswarm.verification.candidate.contracts import RecoveryResultContent
from opsswarm.verification.candidate.store import CandidateRecoveryStore
from tests.api import test_release_execution_jobs_api as execution
from tests.api.test_candidate_conditions_api import replace
from tests.api.test_candidate_recovery_api import (
    PROBE_ORDER_ID,
    PROBE_PATH,
    prepare,
    recovery_worker,
)
from tests.api.test_candidate_recovery_api import (
    current_fixture_time as current_fixture_time,
)
from tests.api.test_candidate_recovery_api import get as recovery_get
from tests.api.test_candidate_recovery_api import wait_context as wait_context
from tests.api.test_human_wait_api import command as wait_command
from tests.api.test_human_wait_api import post as wait_post
from tests.identity_helpers import auth_header

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def get(c, *, run_id=None, headers=None):
    return await c.client.get(
        f"/api/v1/investigations/{run_id or c.started.run.id}/candidate-settlement",
        headers=auth_header("viewer") if headers is None else headers,
    )


async def bind(c):
    c.settlement = CandidateSettlementStore(c.db, clock=c.clock)
    c.app.state.candidate_settlement_store = c.settlement


async def counts(c):
    async with c.db() as session:
        return (
            await session.scalar(text("SELECT count(*) FROM investigation_candidate_settlements")),
            await session.scalar(
                text(
                    "SELECT count(*) FROM agent_task_events "
                    "WHERE event_type='run.candidate_settled'"
                )
            ),
        )


async def frozen_facts(c):
    # 比较持久列而非ORM对象，原run和专业进度事件是本片唯一允许变化的父事实。
    tables = (
        "incidents",
        "incident_events",
        "work_items",
        "investigation_attempts",
        "investigation_terminal_handoffs",
        "investigation_call_reservations",
        "candidate_action_plans",
        "candidate_approval_requests",
        "candidate_approval_decisions",
        "condition_approval_requests",
        "condition_approval_decisions",
        "candidate_release_execution_requests",
        "candidate_release_execution_jobs",
        "candidate_release_execution_attempts",
        "candidate_release_execution_results",
        "candidate_recovery_jobs",
        "candidate_recovery_attempts",
        "candidate_recovery_samples",
        "candidate_recovery_results",
    )
    async with c.db() as session:
        return tuple(
            [
                tuple(
                    await session.scalars(
                        text(f"SELECT to_jsonb(t)::text FROM {table} t ORDER BY to_jsonb(t)::text")
                    )
                )
                for table in tables
            ]
        )


async def recover(c, mode="healthy"):
    calls = []

    async def observe(request):
        assert request.method == "GET" and request.url.path == PROBE_PATH
        calls.append(c.clock())
        if mode == "source_failed":
            raise httpx2.ConnectError("模拟隔离业务来源不可用", request=request)
        if mode == "not_recovered":
            return httpx2.Response(500, json={"detail": "隔离演练订单仍失败"})
        return httpx2.Response(
            200,
            json={
                "order_id": PROBE_ORDER_ID,
                "status": "confirmed",
                "amount": "99.90",
                "currency": "CNY",
            },
        )

    await recovery_worker(c, httpx2.MockTransport(observe)).run_once()
    response = await recovery_get(c)
    assert response.status_code == 200, response.text
    return response.json(), calls


async def history_paths(c, kind):
    base = f"/api/v1/investigations/{c.started.run.id}"
    paths = [
        base,
        base + "/evidence-board",
        base + "/terminal-handoff",
        f"{c.candidate_base}/{c.job_id}",
        c.plan_base,
        c.approval_base,
        c.approval_base + "/decision",
        c.conditions_base,
        c.condition_approval_base,
        c.condition_approval_base + "/decision",
        c.release_base,
        c.execution_base,
        c.recovery_base,
    ]
    if kind == "human_route":
        async with c.db() as session:
            review_id = await session.scalar(text("SELECT id FROM investigation_human_review_jobs"))
        paths.extend(
            [
                c.base,
                c.base + "/commands",
                f"{c.base}/preparations/{c.preparation_id}",
                f"{c.review_base}/{review_id}",
                f"{c.review_base}/{review_id}/route",
            ]
        )
    return paths


@pytest.mark.parametrize(
    ("wait_context", "kind"),
    [("candidate_with_change", "terminal_handoff"), ("human_clean_with_change", "human_route")],
    indirect=["wait_context"],
)
async def test_two_sources_settle_once_preserve_history_and_block_current_writes(
    wait_context, kind
):
    c = wait_context
    if kind == "terminal_handoff":
        executed = await prepare(c)
    else:
        await execution.ready(c, kind=kind)
        c.clock.live = True
        c.recovery = CandidateRecoveryStore(c.db, clock=c.clock)
        c.app.state.candidate_recovery_store = c.recovery
        c.recovery_base = c.execution_base + "/recovery-verification"
        queued = await execution.post(c)
        assert queued.status_code == 201, queued.text
        assert await execution.worker(c, execution.FakeDispatchClient(c)).run_once()
        executed = (await execution.get(c)).json()
        assert executed["result"]["outcome"] == "applied_current", executed
    recovery, calls = await recover(c)
    assert recovery["result"]["outcome"] == "recovered", recovery
    assert len(calls) == 3
    assert all((b - a).total_seconds() >= 2 for a, b in zip(calls, calls[1:], strict=False))
    await bind(c)
    before = await frozen_facts(c)
    old_run = await c.store.get_run(c.started.run.id)
    paths = await history_paths(c, kind)
    old_handoff = (await c.client.get(paths[2], headers=auth_header("viewer"))).json()
    assert (await get(c)).json() == {"record": None}
    assert await counts(c) == (0, 0)
    results = await asyncio.gather(c.settlement.settle_next(), c.settlement.settle_next())
    assert sorted(results) == [False, True]
    response = await get(c)
    assert response.status_code == 200, response.text
    record = response.json()["record"]
    assert record["investigation_run_id"] == str(c.started.run.id)
    assert record["recovery_job_id"] == recovery["record"]["id"]
    assert record["candidate_job_id"] == str(c.job_id)
    assert record["verification_version"] == 1
    assert record["operation_id"] == executed["result"]["operation_id"]
    assert record["execution_result_sha256"] == recovery["record"]["execution_result_sha256"]
    assert record["recovery_result_sha256"] == content_hash(
        RecoveryResultContent.model_validate(recovery["result"])
    )
    assert record["handoff_id"] == str(c.handoff.id)
    assert record["previous_run_status"] == (
        "await_candidate" if kind == "terminal_handoff" else "await_human"
    )
    assert datetime.fromisoformat(record["previous_run_updated_at"]) == old_run.updated_at
    assert record["outcome"] == "recovered" and record["disposition"] == "recovery_verified"
    assert record["policy_version"] == "candidate-settlement-v1"
    assert record["reason_code"] == recovery["result"]["reason_code"]
    assert record["content_sha256"] == content_hash(
        SettlementContent.model_validate(
            {key: value for key, value in record.items() if key != "content_sha256"}
        )
    )
    run = await c.store.get_run(c.started.run.id)
    assert run.status == "settled" and run.completed_at == datetime.fromisoformat(
        record["created_at"]
    )
    assert run.result is run.metrics is run.last_error is None
    assert run.model_dump(exclude={"status", "completed_at", "updated_at"}) == old_run.model_dump(
        exclude={"status", "completed_at", "updated_at"}
    )
    assert await frozen_facts(c) == before
    assert await counts(c) == (1, 1)
    assert not await c.settlement.settle_next()
    assert (await get(c)).json() == response.json()
    events = await c.client.get(
        f"/api/v1/investigations/{run.id}/collaboration/events"
        f"?after_cursor={record['event_cursor'] - 1}",
        headers=auth_header("viewer"),
    )
    assert events.status_code == 200, events.text
    assert [(item["cursor"], item["event_type"]) for item in events.json()] == [
        (record["event_cursor"], "run.candidate_settled")
    ]
    for path in paths:
        historical = await c.client.get(path, headers=auth_header("viewer"))
        assert historical.status_code == 200, (path, historical.text)
    assert (await c.client.get(paths[2], headers=auth_header("viewer"))).json() == old_handoff
    for path in (
        c.approval_base + "/decision",
        c.condition_approval_base + "/decision",
        c.release_base,
    ):
        assert (await c.client.get(path, headers=auth_header("viewer"))).json()[
            "availability"
        ] != "current"
    assert (await recovery_get(c)).json() == recovery
    if kind == "human_route":
        original = wait_command(c, items=[{"kind": "statement", "text": "请继续核对候选依据"}])
        replay = await wait_post(c, original)
        assert replay.status_code == 200 and replay.json()["applied"] is False
        for action in ("supplement", "cancel"):
            rejected = await wait_post(
                c, wait_command(c, action=action, version=2, key=f"settled-{action}-129")
            )
            assert rejected.status_code == 409, rejected.text
    async with c.db() as session:
        index = await session.scalar(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE indexname='uq_investigation_runs_active_incident'"
            )
        )
    assert "settled" in index


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
@pytest.mark.parametrize(
    ("mode", "outcome", "disposition"),
    [
        ("not_recovered", "not_recovered", "reinvestigation_required"),
        ("source_failed", "source_failed", "human_resolution_required"),
        ("unknown", "execution_unknown", "human_resolution_required"),
        ("not_executed", "not_executed", "human_resolution_required"),
        ("superseded", "not_recovered", "human_resolution_required"),
    ],
)
async def test_negative_observations_keep_exact_outcome_without_manufacturing_success(
    wait_context, mode, outcome, disposition
):
    c = wait_context
    await prepare(c, execute=mode in {"not_recovered", "source_failed"})
    if mode in {"unknown", "not_executed", "superseded"}:
        remote = execution.FakeDispatchClient(c)

        async def revoke():
            c.app.state.operator_auth_settings = c.settings.model_copy(
                update={
                    "credentials": tuple(
                        item for item in c.settings.credentials if item.credential_id != "executor"
                    )
                }
            )

        if mode == "unknown":
            remote.after_register = revoke
        elif mode == "not_executed":
            await revoke()
        else:
            original_observation = remote.observation

            def superseded(command):
                observed = original_observation(command)
                if remote.applied_at is None:
                    return observed
                receipt = observed.view.receipt
                state = receipt.current.model_copy(
                    update={
                        "version": "2026.01.01-rc1",
                        "revision": 4,
                        "command_count": 3,
                        "release_500_enabled": True,
                        "updated_at": observed.view.checked_at,
                    }
                )
                return observed.model_copy(
                    update={
                        "view": observed.view.model_copy(
                            update={
                                "receipt": make_receipt(
                                    receipt.idempotency_key, receipt.command, state
                                )
                            }
                        )
                    }
                )

            remote.observation = superseded
        try:
            assert await execution.worker(c, remote).run_once()
        finally:
            c.app.state.operator_auth_settings = c.settings
    recovery, calls = await recover(c, mode)
    assert recovery["result"]["outcome"] == outcome, recovery
    if mode in {"unknown", "not_executed", "superseded"}:
        assert calls == [] and recovery["samples"] == []
    else:
        assert len(calls) == (3 if mode == "not_recovered" else 1)
    await bind(c)
    before = await frozen_facts(c)
    assert await c.settlement.settle_next()
    response = await get(c)
    assert response.status_code == 200, response.text
    record = response.json()["record"]
    assert (record["outcome"], record["disposition"]) == (outcome, disposition)
    assert record["reason_code"] == recovery["result"]["reason_code"]
    assert recovery["result"]["parent_advanced"] is False
    assert (await c.store.get_run(c.started.run.id)).result is None
    assert await frozen_facts(c) == before
    assert await counts(c) == (1, 1)
    assert not await c.settlement.settle_next()


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_trusted_read_corrupt_sources_and_flush_crash_never_leave_partial_settlement(
    wait_context,
):
    c = wait_context
    await prepare(c)
    recovery, _ = await recover(c)
    assert recovery["result"]["outcome"] == "recovered", recovery
    await bind(c)
    before = await frozen_facts(c)
    old_run = await c.store.get_run(c.started.run.id)
    assert (await get(c, headers={})).status_code == 401
    assert (await get(c, headers=auth_header("alert_source"))).status_code == 403
    c.app.state.operator_auth_settings = c.settings.model_copy(update={"mode": "legacy_local"})
    try:
        assert (await get(c, headers={})).status_code == 401
    finally:
        c.app.state.operator_auth_settings = c.settings
    assert (await get(c, run_id="not-a-uuid")).status_code == 422
    assert (await get(c, run_id=uuid4())).status_code == 404
    assert (await get(c)).headers["cache-control"] == "no-store"
    assert (await get(c)).json() == {"record": None}
    assert await counts(c) == (0, 0)
    for table in ("candidate_release_execution_results", "candidate_recovery_results"):
        sql = f"UPDATE {table} SET content_sha256=:value WHERE job_id=:id"
        async with c.db() as session:
            digest = await session.scalar(
                text(f"SELECT content_sha256 FROM {table} WHERE job_id=:id"),
                {"id": UUID(recovery["record"]["id"])},
            )
        try:
            await replace(c, sql, {"id": UUID(recovery["record"]["id"]), "value": "f" * 64})
            with pytest.raises(ValueError):
                await c.settlement.settle_next()
            assert await counts(c) == (0, 0)
            assert await c.store.get_run(c.started.run.id) == old_run
        finally:
            await replace(c, sql, {"id": UUID(recovery["record"]["id"]), "value": digest})
    flushed = False

    def crash_after_flush(session, flush_context):
        nonlocal flushed
        if any(row.__tablename__ == "investigation_candidate_settlements" for row in session.new):
            flushed = True
            raise RuntimeError("模拟收尾已flush、尚未提交时进程失败")

    event.listen(Session, "after_flush", crash_after_flush)
    try:
        with pytest.raises(RuntimeError, match="尚未提交"):
            await c.settlement.settle_next()
    finally:
        event.remove(Session, "after_flush", crash_after_flush)
    assert flushed
    assert await counts(c) == (0, 0)
    assert await c.store.get_run(c.started.run.id) == old_run
    assert await frozen_facts(c) == before
    assert await c.settlement.settle_next()
    saved = (await get(c)).json()
    assert saved["record"] is not None and await counts(c) == (1, 1)
    assert not await c.settlement.settle_next()
    assert (await get(c)).json() == saved
    assert await frozen_facts(c) == before
    # 收尾之后也必须完整重建原材料，不能因父终态已保存而信任缓存。
    for table in ("candidate_release_execution_results", "candidate_recovery_results"):
        identifier = UUID(recovery["record"]["id"])
        async with c.db() as session:
            digest = await session.scalar(
                text(f"SELECT content_sha256 FROM {table} WHERE job_id=:id"),
                {"id": identifier},
            )
        sql = f"UPDATE {table} SET content_sha256=:value WHERE job_id=:id"
        try:
            await replace(c, sql, {"id": identifier, "value": "f" * 64})
            response = await get(c)
            assert response.status_code == 409, response.text
            assert await counts(c) == (1, 1)
        finally:
            await replace(c, sql, {"id": identifier, "value": digest})
    assert (await get(c)).json() == saved
    assert await frozen_facts(c) == before
