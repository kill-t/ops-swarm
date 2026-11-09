"""真实 PG 下从现场人工交接到演练回滚与客观恢复。"""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from email.utils import format_datetime
from types import SimpleNamespace
from uuid import uuid4

import httpx2
import pytest
import pytest_asyncio

from opsswarm.checkpoints import initialize_checkpoint_schema, open_checkpoint_saver
from opsswarm.control_plane.app import create_app
from opsswarm.database import DatabaseSettings
from opsswarm.execution.release.observer import FaultLabReleaseObserver
from opsswarm.execution.release.store import CandidateReleaseRequestStore
from opsswarm.fault_lab.app import create_app as create_lab_app
from opsswarm.fault_lab.controller import FaultController
from opsswarm.fault_lab.release.store import LabReleaseStore
from opsswarm.investigation.budget.contracts import CallBudgetPolicy
from opsswarm.investigation.budget.model_contracts import ModelBudgetPolicy
from opsswarm.investigation.budget.queries import CallBudgetQueries
from opsswarm.investigation.candidate.output import CandidateModelResult, CandidateProposal
from opsswarm.investigation.candidate.tables import CandidateJobRow
from opsswarm.investigation.handoff.persistence import TerminalHandoffStore
from opsswarm.investigation.human_wait.store import HumanWaitStore
from opsswarm.verification.candidate.store import CandidateRecoveryStore
from scripts.local_demo.client import DemoClient
from scripts.local_demo.config import ORDER, ROLES
from scripts.local_demo.disposition import approve_and_verify
from scripts.local_demo.investigation import Investigation
from tests.api import test_candidate_convergence_api as convergence
from tests.api import test_candidate_plan_api as planning
from tests.api import test_candidate_recovery_api as recovery
from tests.api import test_candidate_settlement_api as settlement
from tests.api import test_release_execution_jobs_api as execution
from tests.api.test_candidate_release_request_api import CountedTransport
from tests.api.test_human_reassessment_api import RecordingModel
from tests.identity_helpers import auth_settings, token
from tests.integration.test_live_dynamic_workflow import (
    GRAPH,
    LiveReplies,
    WindowedTelemetry,
    create_live_run,
    make_worker,
)
from tests.integration.test_live_dynamic_workflow import (
    live_query_executor as live_query_executor,
)
from tests.live_dynamic_postgres import (
    live_dynamic_postgres_config as live_dynamic_postgres_config,
)
from tests.live_dynamic_postgres import (
    migrated_database_url as migrated_database_url,
)
from tests.live_task_helpers import LiveHttpFixture
from tests.loop_helpers import loop_policy
from tests.model_budget_helpers import sdk_response

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


class ReleaseChangeFixture(LiveHttpFixture):
    def changes(self, request):
        response = super().changes(request).json()
        deployment = response["items"][-1]
        deployment["change"]["to_version"] = "2026.01.01-rc1"
        response["items"] = [deployment]
        return httpx2.Response(200, json=response)


class ReleaseInvestigationReplies(LiveReplies):
    def __init__(self, db, *, human_wait):
        super().__init__(db)
        self.human_wait = human_wait

    async def __call__(self, request):
        payload = json.loads(request.content)
        data = json.loads(payload["messages"][1]["content"])
        if "policy" in data and "round_number" in data:
            return sdk_response(
                payload,
                {
                    "round_number": 1,
                    "goal": "调查演练发布故障",
                    "tasks": [
                        {
                            "task_key": "change",
                            "role": "change",
                            "goal": "核对发生故障前的部署变更",
                            "lookback_seconds": 21600,
                        }
                    ],
                },
            )
        if "open_hypothesis" in data:
            supporting = data["open_hypothesis"]["hypothesis"]["supporting_evidence_ids"]
            return sdk_response(
                payload,
                {
                    "support_is_sufficient": not self.human_wait,
                    "rationale": "已审查发布记录，可评估候选"
                    if not self.human_wait
                    else "需要工程师核对发布与告警的关系",
                    "accepted_supporting_evidence_ids": [] if self.human_wait else supporting,
                    "counter_evidence_ids": [],
                    "evidence_gaps": ["缺少工程师确认"] if self.human_wait else [],
                    "competing_explanations": [],
                    "suggested_direction": "human_review"
                    if self.human_wait
                    else "evaluate_for_routing",
                    "follow_up_suggestions": [],
                },
            )
        if "working_summaries" in data:
            return sdk_response(
                payload,
                {
                    "statement": "故障可能与本次订单服务发布有关",
                    "confidence": 0.7,
                    "supporting_evidence_ids": [data["evidence"][0]["evidence_id"]],
                    "counter_evidence_ids": [],
                    "unresolved_questions": ["告警与发布的时间关系是否有现场确认？"],
                },
            )
        return await super().__call__(request)


class Clock:
    def __init__(self):
        self.current = datetime.now(UTC)
        self.live = False

    def __call__(self):
        return datetime.now(UTC) if self.live else self.current


@pytest_asyncio.fixture
async def live_handoff_context(
    database_session_factory,
    migrated_database_url,
    checkpoint_database_url,
    live_query_executor,
    request,
):
    db = database_session_factory
    clock = Clock()
    cutoff = clock().replace(microsecond=0) - timedelta(seconds=1)
    budgets = {}
    reflow = getattr(request, "param", None) == "reflow"
    if reflow:
        base = loop_policy()
        # 原运行创建时按 Compose 演练额度约束四阶段输入。
        budgets = {
            "call_budget": CallBudgetPolicy(max_model_calls=14, max_duration_seconds=1800),
            "model_budget": ModelBudgetPolicy.model_validate(
                {
                    **base.model_dump(),
                    "profile": base.profile.model_copy(update={"context_window_tokens": 65536}),
                    "max_input_tokens_per_call": 48000,
                    "max_input_tokens": 500000,
                    "max_estimated_cost_usd": Decimal("20"),
                }
            ),
        }
    store, started = await create_live_run(db, cutoff=cutoff, **budgets)
    await initialize_checkpoint_schema(checkpoint_database_url)
    telemetry = WindowedTelemetry()
    fixture = ReleaseChangeFixture(cutoff, clock, mcp_logs=telemetry, mcp_metrics=telemetry)
    replies = ReleaseInvestigationReplies(db, human_wait=not reflow)
    async with (
        httpx2.AsyncClient(transport=httpx2.MockTransport(replies)) as model,
        open_checkpoint_saver(checkpoint_database_url) as saver,
    ):
        runner = make_worker(db, store, fixture.source, model, saver, clock, live_query_executor)
        assert await runner.run_once()
    handoff = await TerminalHandoffStore(db).get(started.run.id)
    assert handoff is not None, (await store.get_run(started.run.id)).last_error
    assert handoff.destination == ("candidate_evaluation" if reflow else "await_human")
    fixture.assert_windows()

    settings = auth_settings()
    settings = settings.model_copy(
        update={
            "credentials": tuple(
                credential.model_copy(update={"expires_at": clock() + timedelta(days=1)})
                for credential in settings.credentials
            )
        }
    )
    app = create_app(
        database_settings=DatabaseSettings(database_url=migrated_database_url),
        operator_auth_settings=settings,
        clock=clock,
    )
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="http://test"
        ) as client,
    ):
        app.state.human_wait_store = HumanWaitStore(db, clock=clock)
        yield SimpleNamespace(
            db=db,
            app=app,
            client=client,
            clock=clock,
            started=started,
            handoff=handoff,
            base=f"/api/v1/investigations/{started.run.id}/human-wait",
        )


async def rollback(c, *, kind="human_route"):
    await execution.ready(c, kind=kind, real=True)
    if kind == "human_route":
        assert c.artifact.prepared_input.manifest.graph_version == GRAPH
        budget = await CallBudgetQueries(c.db).get_snapshot(c.started.run.id)
        assert budget.deadline == c.artifact.budget.deadline
        assert budget.calls[: len(c.artifact.budget.calls)] == c.artifact.budget.calls
        assert any(call.human_review_attempt_id for call in budget.calls)
    c.recovery = CandidateRecoveryStore(c.db, clock=c.clock)
    c.app.state.candidate_recovery_store = c.recovery
    c.recovery_base = c.execution_base + "/recovery-verification"
    first = await execution.post(c)
    assert first.status_code == 201, first.text
    runner = execution.worker(c, c.dispatch_client)
    assert await runner.run_once()
    return runner, first.json()["job"]["record"]["id"]


async def test_live_human_input_reaches_objective_recovery(live_handoff_context):
    c = live_handoff_context
    runner, job_id = await rollback(c)
    replay = await execution.post(c)
    assert replay.status_code == 200, replay.text
    assert replay.json()["job"]["record"]["id"] == job_id
    assert replay.json()["job"]["result"]["outcome"] == "applied_current", replay.text
    assert not await runner.run_once()
    assert sum(method == "PUT" for method, _ in c.lab_transport.calls) == 1

    assert await recovery.recovery_worker(c, c.lab_transport).run_once()
    view = (await recovery.get(c)).json()
    assert view["result"]["outcome"] == "recovered", view
    assert [item["observation"]["http_status"] for item in view["samples"]] == [200, 200, 200]
    await settlement.bind(c)
    assert await c.settlement.settle_next()
    await convergence.bind(c)
    assert await c.convergence.converge_next()
    incident = (await c.incidents.get_incident(c.started.run.incident_id)).incident
    assert incident.status == "resolved"
    assert (await c.lab_store.get()).version == "stable"


async def test_demo_approval_uses_order_response_time_and_prepares_exact_release(
    live_handoff_context,
):
    c = live_handoff_context
    await planning.ready(c, kind="human_route", run_worker=False)
    async with c.db() as session:
        job = await session.get(CandidateJobRow, c.job_id)
        evidence = [item["evidence_id"] for item in job.source["evidence"]]
    model = RecordingModel(
        result=CandidateModelResult(
            proposal=CandidateProposal(
                status="candidate",
                action_type="rollback_release",
                rationale="复核原发布",
                evidence_ids=evidence,
                preconditions=("确认当前演练版本与发布依据一致",),
            )
        )
    )
    assert await planning.worker(c, model).run_once()

    lab_store = LabReleaseStore(c.db, clock=c.clock)
    await lab_store.initialize()
    await lab_store.set_legacy_fault(enabled=True)
    before = await lab_store.get()
    lab = create_lab_app(FaultController(), lab_release_store=lab_store)
    transport = CountedTransport(lab)
    c.app.state.candidate_release_request_store = CandidateReleaseRequestStore(
        c.db,
        observer=FaultLabReleaseObserver(transport=transport, clock=c.clock),
        clock=c.clock,
    )
    loop = asyncio.get_running_loop()
    observations = []
    events = []

    async with httpx2.AsyncClient(transport=transport, base_url=ORDER) as lab_client:

        async def forward(request):
            path = request.url.path
            if request.url.port == 38001:
                assert request.method == "GET"
                response = await lab_client.get(path)
                if path == "/orders/local-demo":
                    # 进程内 ASGI 由夹具提供等同于服务器的 UTC 响应头。
                    c.clock.current += timedelta(seconds=1)
                    date = format_datetime(c.clock(), usegmt=True)
                    observations.append(date)
                    response.headers["Date"] = date
            else:
                response = await c.client.request(
                    request.method,
                    path,
                    headers=request.headers,
                    content=request.content,
                )
            return httpx2.Response(
                response.status_code, headers=response.headers, content=response.content
            )

        def dispatch(request):
            return asyncio.run_coroutine_threadsafe(forward(request), loop).result(timeout=60)

        client = DemoClient(
            {role: token(role) for role in ROLES}, transport=httpx2.MockTransport(dispatch)
        )
        flow = Investigation(
            client, str(uuid4()), lambda stage, **values: events.append((stage, values))
        )
        flow.base = f"/api/v1/investigations/{c.started.run.id}"
        flow.incident = f"/api/v1/incidents/{c.started.run.incident_id}"
        try:
            version = await asyncio.to_thread(
                approve_and_verify, flow, f"{c.candidate_base}/{c.job_id}"
            )
            decision = await asyncio.to_thread(client.call, version + "/approval-request/decision")
            prepared = await asyncio.to_thread(
                flow.post,
                version + "/release-execution-request",
                dict(
                    expected_incident_version=decision["record"]["incident_version"],
                    approval_decision_id=decision["record"]["id"],
                    approval_content_sha256=decision["record"]["content_sha256"],
                ),
                role="executor",
            )
        finally:
            client.close()

    assert len(observations) == 1
    assert events[0][0] == "approved" and events[0][1]["engineer_approval_rejected"]
    assert prepared["request"]["availability"] == "current"
    assert (await lab_store.get()) == before
