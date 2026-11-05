"""真实 PG 下从现场人工交接到演练回滚与客观恢复。"""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx2
import pytest

from opsswarm.checkpoints import initialize_checkpoint_schema, open_checkpoint_saver
from opsswarm.control_plane.app import create_app
from opsswarm.database import DatabaseSettings
from opsswarm.investigation.budget.queries import CallBudgetQueries
from opsswarm.investigation.handoff.persistence import TerminalHandoffStore
from opsswarm.investigation.human_wait.store import HumanWaitStore
from opsswarm.verification.candidate.store import CandidateRecoveryStore
from tests.api import test_candidate_convergence_api as convergence
from tests.api import test_candidate_recovery_api as recovery
from tests.api import test_candidate_settlement_api as settlement
from tests.api import test_release_execution_jobs_api as execution
from tests.identity_helpers import auth_settings
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
            return sdk_response(
                payload,
                {
                    "support_is_sufficient": False,
                    "rationale": "需要工程师核对发布与告警的关系",
                    "accepted_supporting_evidence_ids": [],
                    "counter_evidence_ids": [],
                    "evidence_gaps": ["缺少工程师确认"],
                    "competing_explanations": [],
                    "suggested_direction": "human_review",
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


async def test_live_human_input_reaches_objective_recovery(
    database_session_factory, migrated_database_url, checkpoint_database_url, live_query_executor
):
    db = database_session_factory
    clock = Clock()
    cutoff = clock().replace(microsecond=0) - timedelta(seconds=1)
    store, started = await create_live_run(db, cutoff=cutoff)
    await initialize_checkpoint_schema(checkpoint_database_url)
    telemetry = WindowedTelemetry()
    fixture = ReleaseChangeFixture(cutoff, clock, mcp_logs=telemetry, mcp_metrics=telemetry)
    async with (
        httpx2.AsyncClient(
            transport=httpx2.MockTransport(ReleaseInvestigationReplies(db))
        ) as model,
        open_checkpoint_saver(checkpoint_database_url) as saver,
    ):
        runner = make_worker(db, store, fixture.source, model, saver, clock, live_query_executor)
        assert await runner.run_once()
    handoff = await TerminalHandoffStore(db).get(started.run.id)
    assert handoff is not None, (await store.get_run(started.run.id)).last_error
    assert handoff.destination == "await_human"
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
        c = SimpleNamespace(
            db=db,
            app=app,
            client=client,
            clock=clock,
            started=started,
            handoff=handoff,
            base=f"/api/v1/investigations/{started.run.id}/human-wait",
        )
        await execution.ready(c, kind="human_route", real=True)
        assert c.artifact.prepared_input.manifest.graph_version == GRAPH
        budget = await CallBudgetQueries(db).get_snapshot(started.run.id)
        assert budget.deadline == c.artifact.budget.deadline
        assert budget.calls[: len(c.artifact.budget.calls)] == c.artifact.budget.calls
        assert any(call.human_review_attempt_id for call in budget.calls)
        c.recovery = CandidateRecoveryStore(db, clock=clock)
        c.app.state.candidate_recovery_store = c.recovery
        c.recovery_base = c.execution_base + "/recovery-verification"
        first = await execution.post(c)
        assert first.status_code == 201, first.text
        runner = execution.worker(c, c.dispatch_client)
        assert await runner.run_once()
        replay = await execution.post(c)
        assert replay.status_code == 200, replay.text
        assert replay.json()["job"]["record"]["id"] == first.json()["job"]["record"]["id"]
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
        incident = (await c.incidents.get_incident(started.run.incident_id)).incident
        assert incident.status == "resolved"
        assert (await c.lab_store.get()).version == "stable"
