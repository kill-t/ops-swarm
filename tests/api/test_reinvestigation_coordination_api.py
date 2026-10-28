"""真实原预算与独立回流阶段的API、Worker和历史隔离。"""

import asyncio
import json
import sys
from decimal import Decimal

import pytest
from sqlalchemy import text

from opsswarm.investigation.budget.model_contracts import ReportedModelUsage
from opsswarm.investigation.reinvestigation_coordination.contracts import ReflowModelResult
from opsswarm.investigation.reinvestigation_coordination.job_store import ReflowJobStore
from opsswarm.investigation.reinvestigation_coordination.ledger import ReflowLedger
from opsswarm.investigation.reinvestigation_coordination.queries import ReflowQueries
from opsswarm.investigation.reinvestigation_coordination.worker import ReflowWorker
from tests.api import test_reinvestigation_admission_api as admission
from tests.api.test_reinvestigation_admission_api import (
    current_fixture_time as current_fixture_time,
)
from tests.api.test_reinvestigation_admission_api import wait_context as wait_context
from tests.identity_helpers import auth_header
from tests.integration import test_dynamic_worker_handoff as worker_fixture

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture(autouse=True)
def initial_reflow_capacity(monkeypatch, request, current_fixture_time):
    """正向夹具从根创建就保留完整输入容量，不追加既有运行预算。"""
    del current_fixture_time
    if "original_context_limit" in request.node.name:
        return
    original = worker_fixture.loop_policy

    def policy():
        original_policy = original()
        return original_policy.model_copy(
            update={
                "profile": original_policy.profile.model_copy(
                    update={"context_window_tokens": 262144}
                ),
                "max_input_tokens_per_call": 200000,
                "max_input_tokens": 3000000,
                "max_estimated_cost_usd": Decimal("20"),
            }
        )

    monkeypatch.setattr(worker_fixture, "loop_policy", policy)


async def prepare(c, kind="terminal_handoff"):
    await admission.prepare(c, kind=kind)
    response = await admission.post(c)
    assert response.status_code == 201, response.text
    c.reflow_store = ReflowJobStore(c.db, clock=c.clock)
    c.reflow_ledger = ReflowLedger(c.db, clock=c.clock)
    c.reflow_errors = []
    original_fail = c.reflow_store.fail

    async def record_failure(*args, **kwargs):
        c.reflow_errors.append(str(sys.exception()))
        return await original_fail(*args, **kwargs)

    c.reflow_store.fail = record_failure
    c.reflow_queries = ReflowQueries(c.db, clock=c.clock)
    c.app.state.reflow_queries = c.reflow_queries
    c.reflow_base = f"/api/v1/investigations/{c.started.run.id}/reinvestigation-coordination"


async def get(c, *, headers=None, suffix=""):
    return await c.client.get(
        c.reflow_base + suffix, headers=auth_header("viewer") if headers is None else headers
    )


class FakeModel:
    def __init__(self, *, hook=None, usage=True):
        self.calls = []
        self.hook = hook
        self.usage = (
            ReportedModelUsage(input_tokens=80, output_tokens=20, total_tokens=100)
            if usage
            else None
        )

    async def invoke(self, model_input):
        self.calls.append(model_input)
        payload = json.loads(model_input.messages[1].content)
        if self.hook:
            await self.hook()
        return ReflowModelResult.model_validate(
            {
                "proposal": {
                    "round_number": payload["next_round_number"],
                    "goal": "调查处置后业务仍失败的原因",
                    "tasks": [
                        {
                            "task_key": "verify_remaining_failure",
                            "role": "log",
                            "goal": "读取新日志并核对未恢复路径和反证",
                            "lookback_seconds": 300,
                        }
                    ],
                }
            }
        )


def worker(c, model):
    return ReflowWorker(
        c.reflow_store, c.reflow_ledger, model_factory=lambda _: model, worker_id="reflow-test-132"
    )


async def calls(c):
    async with c.db() as session:
        return list(
            await session.scalars(
                text(
                    "SELECT to_jsonb(t) FROM investigation_call_reservations t "
                    "WHERE investigation_run_id=:id ORDER BY sequence"
                ),
                {"id": c.started.run.id},
            )
        )


async def root_facts(c):
    result = []
    async with c.db() as session:
        for table in (
            "investigation_runs",
            "work_items",
            "investigation_attempts",
            "investigation_rounds",
            "agent_tasks",
            "investigation_terminal_handoffs",
            "candidate_recovery_results",
            "investigation_candidate_settlements",
            "candidate_incident_convergences",
            "candidate_reinvestigation_admissions",
        ):
            result.append(
                tuple(
                    await session.scalars(
                        text(f"SELECT to_jsonb(t)::text FROM {table} t ORDER BY to_jsonb(t)::text")
                    )
                )
            )
    return tuple(result)


@pytest.mark.parametrize(
    ("wait_context", "kind"),
    [("candidate_with_change", "terminal_handoff"), ("human_clean_with_change", "human_route")],
    indirect=["wait_context"],
)
async def test_two_sources_reserve_original_budget_and_save_undispatched_proposal(
    wait_context, kind
):
    c = wait_context
    await prepare(c, kind)
    before = await root_facts(c)
    old_calls = await calls(c)
    assert (await get(c)).json()["status"] == "pending"
    assert (await get(c, headers={})).status_code == 401
    prepared = await asyncio.gather(c.reflow_store.prepare_next(), c.reflow_store.prepare_next())
    assert sorted(prepared) == [False, True]
    queued = await get(c)
    assert queued.status_code == 200, queued.text
    assert queued.json()["status"] == "queued", queued.text

    async def inflight():
        assert not await worker(c, FakeModel()).run_once()
        history = await admission.get(c)
        assert history.status_code == 200, history.text
        assert history.json()["availability"] == "blocked"
        assert "calls_in_flight" in history.json()["blocking_reason_codes"]
        stored = await calls(c)
        call_id = stored[-1]["id"]
        for suffix in ("model-input", "interaction", "context-manifest"):
            path = f"/api/v1/investigations/{c.started.run.id}/calls/{call_id}/{suffix}"
            response = await c.client.get(path, headers=auth_header("viewer"))
            assert response.status_code == 200, (path, response.text)
            c.app.state.operator_auth_settings = c.settings.model_copy(
                update={"mode": "legacy_local"}
            )
            try:
                assert (await c.client.get(path)).status_code == 401
                assert (await c.client.get(path, headers=auth_header("viewer"))).status_code == 401
                old_path = (
                    f"/api/v1/investigations/{c.started.run.id}/calls/"
                    f"{old_calls[0]['id']}/model-input"
                )
                assert (await c.client.get(old_path)).status_code == 200
            finally:
                c.app.state.operator_auth_settings = c.settings

    model = FakeModel(hook=inflight, usage=False)
    assert await worker(c, model).run_once()
    response = await get(c)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "succeeded", (body, c.reflow_errors)
    assert body["result"]["dispatch_status"] == "not_dispatched"
    assert len(model.calls) == 1
    assert await root_facts(c) == before
    new_calls = await calls(c)
    assert new_calls[:-1] == old_calls and len(new_calls) == len(old_calls) + 1
    assert new_calls[-1]["operation"] == "coordinate_reinvestigation"
    assert new_calls[-1]["model_usage"] is None and new_calls[-1]["status"] == "succeeded"
    assert (await get(c, suffix="/input")).json()["packet"] == body["packet"]
    assert not await worker(c, model).run_once()


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_successful_call_is_reused_after_result_save_crash(wait_context, monkeypatch):
    c = wait_context
    await prepare(c)
    before = await root_facts(c)
    original = c.reflow_store.complete

    async def crash(*args):
        raise RuntimeError("模拟成功结算后业务结果落库前崩溃")

    monkeypatch.setattr(c.reflow_store, "complete", crash)
    model = FakeModel()
    assert await worker(c, model).run_once()
    assert len(model.calls) == 1
    monkeypatch.setattr(c.reflow_store, "complete", original)
    await asyncio.sleep(2)
    assert await worker(c, model).run_once()
    response = await get(c)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "succeeded", response.text
    assert len(model.calls) == 1
    assert await root_facts(c) == before


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_original_context_limit_blocks_without_adding_calls(wait_context):
    c = wait_context
    await prepare(c)
    before = await calls(c)
    model = FakeModel()
    assert await worker(c, model).run_once()
    response = await get(c)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "blocked", response.text
    assert response.json()["error_code"] == "model_input_budget_exceeded"
    assert not model.calls and await calls(c) == before


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_new_alert_during_model_call_keeps_unknown_allocation_and_no_result(wait_context):
    c = wait_context
    await prepare(c)
    before = await root_facts(c)
    old = await calls(c)

    async def new_alert():
        await admission.attach_old_alert(c)

    model = FakeModel(hook=new_alert)
    assert await worker(c, model).run_once()
    response = await get(c)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "failed", (response.text, c.reflow_errors)
    assert response.json()["result"] is None
    stored = await calls(c)
    assert len(model.calls) == 1 and stored[:-1] == old
    assert stored[-1]["status"] == "unknown" and stored[-1]["call_result"] is None
    assert stored[-1]["model_allocation"] is not None
    assert (await admission.get(c)).status_code == 200
    assert await root_facts(c) == before


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_blocked_without_input_still_validates_original_admission(wait_context):
    c = wait_context
    await prepare(c)
    before = await calls(c)
    await admission.attach_old_alert(c)
    model = FakeModel()
    assert await worker(c, model).run_once()
    response = await get(c)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "blocked"
    assert response.json()["error_code"] == "reflow_source_changed"
    assert response.json()["packet"] is None
    assert not model.calls and await calls(c) == before
    async with c.db() as session:
        original_hash = await session.scalar(
            text(
                "SELECT content_sha256 FROM candidate_reinvestigation_admissions "
                "WHERE investigation_run_id=:id"
            ),
            {"id": c.started.run.id},
        )
    sql = (
        "UPDATE candidate_reinvestigation_admissions SET content_sha256=:hash "
        "WHERE investigation_run_id=:id"
    )
    try:
        await admission.replace(c, sql, {"hash": "f" * 64, "id": c.started.run.id})
        damaged = await get(c)
        assert damaged.status_code == 409, damaged.text
    finally:
        await admission.replace(c, sql, {"hash": original_hash, "id": c.started.run.id})
    assert (await get(c)).status_code == 200
