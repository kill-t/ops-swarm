"""用真实 PostgreSQL 和进程内故障服务覆盖一次失败再调查。"""

import json
from datetime import datetime
from types import SimpleNamespace

import httpx2
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from opsswarm.investigation.budget.queries import CallBudgetQueries
from opsswarm.investigation.changes import ChangeApiOnlyInvestigationDataSource
from opsswarm.investigation.reinvestigation_candidate.job_store import ReflowCandidateJobStore
from opsswarm.investigation.reinvestigation_coordination.job_store import ReflowJobStore
from opsswarm.investigation.reinvestigation_coordination.ledger import ReflowLedger
from opsswarm.investigation.reinvestigation_dispatch.store import DispatchStore
from opsswarm.investigation.reinvestigation_execution.evidence import ExecutionEvidenceSource
from opsswarm.investigation.reinvestigation_execution.ledger import ExecutionLedger
from opsswarm.investigation.reinvestigation_execution.store import ExecutionStore
from opsswarm.investigation.reinvestigation_review.job_store import ReflowReviewJobStore
from opsswarm.investigation.reinvestigation_review.ledger import ReflowReviewLedger
from opsswarm.investigation.reinvestigation_routing.store import ReflowRoutingStore
from opsswarm.investigation.reinvestigation_synthesis.job_store import SynthesisJobStore
from opsswarm.investigation.reinvestigation_synthesis.ledger import SynthesisLedger
from tests.api import test_candidate_convergence_api as convergence
from tests.api import test_candidate_recovery_api as recovery
from tests.api import test_candidate_settlement_api as settlement
from tests.api import test_reinvestigation_admission_api as admission
from tests.api import test_reinvestigation_coordination_api as coordination
from tests.api import test_reinvestigation_execution_api as execution
from tests.api import test_reinvestigation_review_api as review
from tests.api import test_reinvestigation_routing_api as routing
from tests.api import test_reinvestigation_synthesis_api as synthesis
from tests.api import test_release_execution_jobs_api as release
from tests.api.test_live_human_continuation_api import (
    live_handoff_context as live_handoff_context,
)
from tests.api.test_live_human_continuation_api import rollback
from tests.integration.test_live_dynamic_workflow import (
    live_query_executor as live_query_executor,
)
from tests.live_dynamic_postgres import (
    live_dynamic_postgres_config as live_dynamic_postgres_config,
)
from tests.live_dynamic_postgres import (
    migrated_database_url as migrated_database_url,
)
from tests.unit.investigation import test_changes as changes_fixture

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def observe_failed_recovery(c):
    await rollback(c, kind="terminal_handoff")
    dispatch = await release.get(c)
    assert dispatch.status_code == 200 and dispatch.json()["result"] is not None, dispatch.text
    assert dispatch.json()["result"]["outcome"] == "applied_current", dispatch.text
    async with httpx2.AsyncClient(
        transport=c.lab_transport, base_url="http://order-service"
    ) as client:
        changed = await client.put(
            "/__admin/configuration/database-pool", json={"value": 1, "expected_value": 50}
        )
    assert changed.status_code == 200, changed.text
    claimed = await recovery.recovery_worker(c, c.lab_transport).run_once()
    view = (await recovery.get(c)).json()
    assert claimed, view
    assert view["result"]["outcome"] == "not_recovered", view
    assert [sample["observation"]["http_status"] for sample in view["samples"]] == [500, 500, 500]
    await settlement.bind(c)
    assert await c.settlement.settle_next()
    await convergence.bind(c)
    assert await c.convergence.converge_next()
    return datetime.fromisoformat(view["result"]["created_at"])


class PostVerificationChanges:
    def __init__(self, clock):
        self.clock = clock
        self.windows = []

    def __call__(self, request):
        assert request.url.path == "/api/v1/changes/window"
        start = request.url.params["window_start"]
        end = request.url.params["window_end"]
        self.windows.append((datetime.fromisoformat(start), datetime.fromisoformat(end)))
        payload = changes_fixture._response(generated_at=self.clock()).model_dump(mode="json")
        pool = payload["items"][1]
        pool["occurred_at"] = pool["recorded_at"] = start
        pool["change"]["to_value"] = 1
        payload["items"] = [pool]
        payload["window"] = {"window_start": start, "window_end": end}
        return httpx2.Response(200, json=payload)


async def run_reflow(c, change):
    workers = SimpleNamespace(
        reflow_store=ReflowJobStore(c.db, clock=c.clock),
        reflow_ledger=ReflowLedger(c.db, clock=c.clock),
        execution_store=ExecutionStore(c.db, clock=c.clock),
        execution_ledger=ExecutionLedger(c.db, clock=c.clock),
        synthesis_store=SynthesisJobStore(c.db, clock=c.clock),
        synthesis_ledger=SynthesisLedger(c.db, clock=c.clock),
        review_store=ReflowReviewJobStore(c.db, clock=c.clock),
        review_ledger=ReflowReviewLedger(c.db, clock=c.clock),
    )
    coord_model = execution.CoordinationModel(("change",))
    assert await coordination.worker(workers, coord_model).run_once()
    assert await DispatchStore(c.db, clock=c.clock).dispatch_next()
    evidence = ExecutionEvidenceSource(
        changes=ChangeApiOnlyInvestigationDataSource(
            transport=httpx2.MockTransport(change), clock=lambda: c.clock().timestamp()
        )
    )
    try:
        assert await execution.worker(workers, evidence, execution.FakeModel()).run_once()
    finally:
        evidence.close()
    assert await synthesis.worker(workers, synthesis.Model()).run_once()
    assert await review.worker(workers, review.Model()).run_once()


@pytest.mark.parametrize("live_handoff_context", ["reflow"], indirect=True)
async def test_live_failure_reinvestigates_once_then_hands_off(live_handoff_context):
    c = live_handoff_context
    floor = await observe_failed_recovery(c)
    original_calls = tuple(c.lab_transport.calls)
    await admission.bind(c)
    body = await admission.command(c)
    created = await admission.post(c, body)
    assert created.status_code == 201, created.text
    replay = await admission.post(c, body)
    assert replay.status_code == 200, replay.text
    denied = await admission.post(c, {**body, "idempotency_key": "second-reflow-command"})
    assert denied.status_code == 409 and "reinvestigation_already_exists" in denied.text

    budgets = CallBudgetQueries(c.db)
    before = await budgets.get_snapshot(c.started.run.id)
    change = PostVerificationChanges(c.clock)
    await run_reflow(c, change)
    assert len(change.windows) == 1 and floor < change.windows[0][0] < change.windows[0][1]

    async with c.db() as session:
        packets = await session.scalars(
            text(
                "SELECT packet FROM investigation_reflow_jobs WHERE investigation_run_id=:run "
                "UNION ALL SELECT packet FROM investigation_reflow_task_packets "
                "WHERE investigation_run_id=:run "
                "UNION ALL SELECT packet FROM investigation_reflow_synthesis_jobs "
                "WHERE investigation_run_id=:run "
                "UNION ALL SELECT packet FROM investigation_reflow_review_jobs "
                "WHERE investigation_run_id=:run"
            ),
            {"run": c.started.run.id},
        )
        packets = packets.all()
        native = await routing.native_content(c, session)
    assert len(packets) == 4
    for packet in packets:
        assert packet["allocation"]["estimated_input_tokens"] <= 48000
        payload = json.loads(packet["model_input"]["messages"][1]["content"])
        history = next(
            item["content"]
            for item in payload.get("source", payload)["materials"]
            if item["kind"] == "original_investigation"
        )
        assert history["input_policy"] == "reflow-projection-v1"
        assert history["evidence"] and history["working_summaries"]
        assert "prior_input" not in history
    assert native["route"] == "await_human"
    with pytest.raises(DBAPIError, match="路由"):
        async with c.db() as session, session.begin():
            await routing.insert(session, {**native, "route": "candidate_evaluation"})
    store = ReflowRoutingStore(c.db, clock=c.clock)
    assert await store.record_next()
    assert not await store.record_next()
    c.routing_base = f"/api/v1/investigations/{c.started.run.id}/reinvestigation-routing"
    response = await routing.get(c)
    assert response.status_code == 200, response.text
    handoff = response.json()["record"]
    assert handoff["base_decision"]["route"] == "candidate_evaluation"
    assert handoff["route"] == "await_human"
    assert handoff["dispatch_status"] == "not_dispatched"
    assert "live_reflow_manual_takeover" in handoff["blocking_reason_codes"]
    assert not await ReflowCandidateJobStore(c.db, clock=c.clock).prepare_next()
    budget = await budgets.get_snapshot(c.started.run.id)
    assert budget.deadline == before.deadline and budget.calls[: len(before.calls)] == before.calls
    assert len(budget.calls) > len(before.calls)
    assert all(call.status != "reserved" for call in budget.calls)
    assert await convergence.active_slot(c) == 1
    incident = (await c.incidents.get_incident(c.started.run.incident_id)).incident
    assert incident.status == "investigating"
    assert tuple(c.lab_transport.calls) == original_calls
