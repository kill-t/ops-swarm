"""真实分派和原预算上的专业执行、恢复及历史隔离。"""

import asyncio
import json
from datetime import timedelta

import pytest
from sqlalchemy import text

from opsswarm.investigation.budget.model_contracts import ReportedModelUsage
from opsswarm.investigation.reinvestigation_coordination.contracts import ReflowModelResult
from opsswarm.investigation.reinvestigation_execution.contracts import ExecutionModelResult
from opsswarm.investigation.reinvestigation_execution.ledger import ExecutionLedger
from opsswarm.investigation.reinvestigation_execution.queries import ExecutionQueries
from opsswarm.investigation.reinvestigation_execution.store import ExecutionStore
from opsswarm.investigation.reinvestigation_execution.worker import ExecutionWorker
from opsswarm.investigation.tasks.evidence import TaskEvidenceSnapshot
from tests.api import test_reinvestigation_dispatch_api as dispatch
from tests.api.test_reinvestigation_dispatch_api import current_fixture_time as current_fixture_time
from tests.api.test_reinvestigation_dispatch_api import (
    initial_reflow_capacity as initial_reflow_capacity,
)
from tests.api.test_reinvestigation_dispatch_api import wait_context as wait_context
from tests.identity_helpers import auth_header
from tests.unit.investigation.test_reinvestigation_execution_materials import (
    observation,
    report_for,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


class CoordinationModel(dispatch.coordination.FakeModel):
    def __init__(self, roles):
        super().__init__()
        self.roles = roles

    async def invoke(self, model_input):
        result = await super().invoke(model_input)
        proposal = result.proposal.model_dump(mode="json")
        proposal["tasks"] = [
            dict(
                task_key=f"followup_{role}",
                role=role,
                goal=f"调查{role}未恢复路径",
                lookback_seconds=300,
            )
            for role in self.roles
        ]
        return ReflowModelResult.model_validate({"proposal": proposal})


class FakeEvidence:
    def __init__(self, hook=None):
        self.calls = []
        self.hook = hook

    async def collect(self, assignment, task_id, window):
        self.calls.append(task_id)
        if self.hook:
            await self.hook()
        return TaskEvidenceSnapshot(
            version="task-evidence-live-mcp-v1",
            data_source_mode="live",
            evidence_snapshot_id=f"reflow-v1:{task_id}",
            role=assignment.role,
            service_name=assignment.service_name,
            window_start=window.window.window_start,
            window_end=window.window.window_end,
            evidence=(observation(assignment, window),),
        )


class FakeModel:
    def __init__(self, hook=None, usage=False):
        self.calls = []
        self.hook = hook
        self.usage = (
            ReportedModelUsage(input_tokens=80, output_tokens=20, total_tokens=100)
            if usage
            else None
        )

    async def invoke(self, model_input):
        self.calls.append(model_input)
        body = json.loads(model_input.messages[1].content)
        if self.hook:
            await self.hook()
        return ExecutionModelResult(
            report=report_for(TaskEvidenceSnapshot.model_validate(body["new_evidence"]))
        )


async def prepare(c, kind="terminal_handoff", roles=("log",), *, wait_window=True):
    await dispatch.prepare(c, kind, model=CoordinationModel(roles))
    if wait_window:
        async with c.db() as session:
            floor = await session.scalar(
                text(
                    "SELECT (m#>>'{content,result,created_at}')::timestamptz "
                    "FROM investigation_reflow_jobs j CROSS JOIN LATERAL "
                    "jsonb_array_elements(j.source->'materials') m "
                    "WHERE j.id=:id AND m->>'kind'='recovery'"
                ),
                {"id": c.started.run.id},
            )
        # 为旧MCP整分钟契约实际等待，原deadline和任何已冻结事实均不修改。
        while (seconds := (floor + timedelta(seconds=65) - c.clock()).total_seconds()) > 0:
            await asyncio.sleep(min(1, seconds))
    assert await c.dispatch_store.dispatch_next()
    response = await dispatch.get(c)
    assert response.status_code == 200 and response.json()["record"]["status"] == "queued", (
        response.text
    )
    c.execution_store = ExecutionStore(c.db, clock=c.clock)
    c.execution_ledger = ExecutionLedger(c.db, clock=c.clock)
    c.app.state.execution_queries = ExecutionQueries(c.db, clock=c.clock)
    c.execution_base = f"/api/v1/investigations/{c.started.run.id}/reinvestigation-execution"
    c.execution_errors = []
    original = c.execution_store.fail

    async def record_failure(*args, **kwargs):
        import sys

        c.execution_errors.append(str(sys.exception()))
        return await original(*args, **kwargs)

    c.execution_store.fail = record_failure


async def get(c, headers=None):
    return await c.client.get(
        c.execution_base, headers=auth_header("viewer") if headers is None else headers
    )


def worker(c, evidence, model):
    return ExecutionWorker(
        c.execution_store,
        c.execution_ledger,
        evidence,
        model_factory=lambda _: model,
        worker_id="execution-test-134",
    )


@pytest.mark.parametrize(
    ("wait_context", "kind"),
    [("candidate_with_change", "terminal_handoff"), ("human_clean_with_change", "human_route")],
    indirect=["wait_context"],
)
async def test_three_roles_execute_on_original_ledger_and_keep_both_histories(wait_context, kind):
    c = wait_context
    await prepare(c, kind, roles=("log", "metric", "change"))
    before, calls = await dispatch.coordination.root_facts(c), await dispatch.coordination.calls(c)
    old_dispatch = (await dispatch.get(c)).json()
    assert (await get(c, {})).status_code == 401
    assert (await get(c, auth_header("alert_source"))).status_code == 403
    evidence = FakeEvidence()

    async def inflight():
        history = await dispatch.get(c)
        assert history.status_code == 200, history.text
        assert history.json()["record"] == old_dispatch["record"]
        assert history.json()["tasks"] == old_dispatch["tasks"]
        current = await dispatch.coordination.calls(c)
        call_id = current[-1]["id"]
        for suffix in ("model-input", "context-manifest", "interaction"):
            path = f"/api/v1/investigations/{c.started.run.id}/calls/{call_id}/{suffix}"
            response = await c.client.get(path, headers=auth_header("viewer"))
            assert response.status_code == 200, (path, response.text)

    model = FakeModel(hook=inflight)
    for _ in range(3):
        assert await worker(c, evidence, model).run_once()
    response = await get(c)
    assert response.status_code == 200, response.text
    body = response.json()
    assert {t["status"] for t in body["tasks"]} == {"succeeded"}, (body, c.execution_errors)
    assert len(evidence.calls) == len(model.calls) == 3
    for t in body["tasks"]:
        assert t["result"]["report"]["evidence_ids"] == [
            t["evidence"]["evidence"][0]["evidence_id"]
        ]
        assert (
            len(
                json.loads(t["packet"]["model_input"]["messages"][1]["content"])["source"][
                    "materials"
                ]
            )
            == 10
        )
    stored = await dispatch.coordination.calls(c)
    assert len(stored) == len(calls) + 6 and stored[: len(calls)] == calls
    assert {s["reflow_task_id"] for s in stored[len(calls) :]} == set(
        str(t) for t in evidence.calls
    )
    assert all(s["model_usage"] is None for s in stored[len(calls) :] if s["kind"] == "model")
    assert await dispatch.coordination.root_facts(c) == before
    assert (await dispatch.get(c)).json()["record"] == old_dispatch["record"]
    assert not await worker(c, evidence, model).run_once()


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_successful_evidence_and_model_are_reused_after_publish_crash(
    wait_context, monkeypatch
):
    c = wait_context
    await prepare(c)
    evidence, model = FakeEvidence(), FakeModel(usage=True)
    original = c.execution_store.complete

    async def crash(*args):
        raise RuntimeError("模拟已结算后发布前崩溃")

    monkeypatch.setattr(c.execution_store, "complete", crash)
    assert await worker(c, evidence, model).run_once()
    assert len(evidence.calls) == len(model.calls) == 1
    monkeypatch.setattr(c.execution_store, "complete", original)
    await asyncio.sleep(2)
    assert await worker(c, evidence, model).run_once()
    response = await get(c)
    assert response.status_code == 200, response.text
    assert response.json()["tasks"][0]["status"] == "succeeded", (response.text, c.execution_errors)
    assert len(evidence.calls) == len(model.calls) == 1
    assert len(response.json()["tasks"][0]["attempts"]) == 2


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_new_alert_during_model_call_audits_unknown_without_publishing(wait_context):
    c = wait_context
    await prepare(c)

    async def new_alert():
        await dispatch.coordination.admission.attach_old_alert(c)

    evidence, model = FakeEvidence(), FakeModel(hook=new_alert, usage=True)
    assert await worker(c, evidence, model).run_once()
    response = await get(c)
    assert response.status_code == 200, response.text
    task = response.json()["tasks"][0]
    assert task["status"] == "failed" and task["result"] is None, (
        response.text,
        c.execution_errors,
    )
    assert task["evidence"] is not None
    calls = await dispatch.coordination.calls(c)
    assert calls[-1]["status"] == "unknown" and calls[-1]["model_usage"]["input_tokens"] == 80
    assert (await dispatch.get(c)).json()["availability"] == "stale"
