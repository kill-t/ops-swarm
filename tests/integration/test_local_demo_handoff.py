"""使用正式脚本、真实 PostgreSQL 和固定模型验证完整冻结人工续接。"""

import asyncio
import json
from contextlib import AsyncExitStack
from datetime import datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import httpx2
import pytest
from dotenv import dotenv_values
from langchain_openai import ChatOpenAI

from opsswarm.checkpoints import initialize_checkpoint_schema, open_checkpoint_saver
from opsswarm.control_plane.app import create_app
from opsswarm.database import DatabaseSettings
from opsswarm.identity.settings import OperatorAuthSettings
from opsswarm.investigation.budget.queries import CallBudgetQueries
from opsswarm.investigation.candidate.job_store import CandidateJobStore
from opsswarm.investigation.candidate.ledger import CandidateLedger
from opsswarm.investigation.candidate.model import CandidateModel
from opsswarm.investigation.candidate.worker import CandidateWorker
from opsswarm.investigation.handoff.persistence import TerminalHandoffStore
from opsswarm.investigation.human_prepare.job_store import HumanPreparationJobStore
from opsswarm.investigation.human_prepare.worker import HumanPreparationWorker
from opsswarm.investigation.human_review.job_store import HumanReviewJobStore
from opsswarm.investigation.human_review.ledger import HumanReviewLedger
from opsswarm.investigation.human_review.model import HumanReviewModel
from opsswarm.investigation.human_review.worker import HumanReviewWorker
from opsswarm.investigation.persistence import InvestigationStore
from opsswarm.investigation.review_routing.queries import ReviewRoutingQueries
from opsswarm.observability import TracingRuntime
from opsswarm.worker.dynamic_factory import CurrentDynamicGraphFactory
from opsswarm.worker.dynamic_processor import DynamicInvestigationProcessor
from opsswarm.worker.runner import InvestigationWorker
from scripts.local_demo.__main__ import execute
from scripts.local_demo.client import DemoClient
from scripts.local_demo.config import CHANGE, MODEL, ORDER, prepare, read_config
from scripts.local_demo.fixture import completion
from tests.live_dynamic_postgres import (
    live_dynamic_postgres_config as live_dynamic_postgres_config,
)
from tests.live_dynamic_postgres import (
    migrated_database_url as migrated_database_url,
)
from tests.task_facts_helpers import TaskClock
from tests.unit.test_local_demo import demo_dir as demo_dir

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_frozen_demo_survives_api_replacement_and_reaches_candidate(
    demo_dir, database_session_factory, migrated_database_url, checkpoint_database_url
):
    db, clock = database_session_factory, TaskClock()
    await initialize_checkpoint_schema(checkpoint_database_url)
    env = prepare(demo_dir)
    auth = OperatorAuthSettings(
        mode="bearer",
        credentials=json.loads(dotenv_values(env)["OPSSWARM_OPERATOR_AUTH_CREDENTIALS"]),
    )
    loop = asyncio.get_running_loop()
    restarts = []
    writes = []
    model_errors = []

    def model_reply(request):
        try:
            return httpx2.Response(200, json=completion(json.loads(request.content)))
        except (ValueError, KeyError, IndexError, TypeError) as error:
            model_errors.append(repr(error))
            raise

    async with (
        httpx2.AsyncClient(transport=httpx2.MockTransport(model_reply)) as model_client,
        open_checkpoint_saver(checkpoint_database_url) as saver,
        AsyncExitStack() as api_stack,
    ):
        model = ChatOpenAI(
            api_key="test-only-key",
            model=MODEL,
            base_url="https://model.invalid/v1",
            http_async_client=model_client,
            use_responses_api=False,
            max_retries=0,
            n=1,
        )

        def new_investigation_worker():
            return InvestigationWorker(
                store=InvestigationStore(db),
                processor=DynamicInvestigationProcessor(
                    factory=CurrentDynamicGraphFactory(
                        session_factory=db,
                        model=model,
                        checkpointer=saver,
                        clock=clock,
                    ),
                    model_name=MODEL,
                ),
                worker_id=f"demo-investigation-{len(restarts)}",
                clock=clock,
                handoff_store=TerminalHandoffStore(db),
                model_name=MODEL,
                lease_duration=timedelta(seconds=360),
                processing_timeout=timedelta(seconds=300),
            )

        investigation = new_investigation_worker()
        preparation = HumanPreparationWorker(
            HumanPreparationJobStore(db, clock=clock), worker_id="demo-preparation"
        )
        review = HumanReviewWorker(
            HumanReviewJobStore(db, clock=clock),
            HumanReviewLedger(db, clock=clock),
            model_factory=lambda policy: HumanReviewModel(model, policy),
            worker_id="demo-review",
        )
        candidate = CandidateWorker(
            CandidateJobStore(db, clock=clock),
            CandidateLedger(db, clock=clock),
            model_factory=lambda policy: CandidateModel(model, policy),
            worker_id="demo-candidate",
        )

        async def open_api():
            app = create_app(
                database_settings=DatabaseSettings(database_url=migrated_database_url),
                operator_auth_settings=auth,
                tracing_runtime=TracingRuntime(),
                clock=clock,
            )
            await api_stack.enter_async_context(app.router.lifespan_context(app))
            return await api_stack.enter_async_context(
                httpx2.AsyncClient(
                    transport=httpx2.ASGITransport(app=app),
                    base_url="http://testserver",
                )
            )

        api = await open_api()

        async def replace_api():
            nonlocal api, investigation
            await api_stack.aclose()
            restarts.append(True)
            investigation = new_investigation_worker()
            api = await open_api()

        async def forward(request):
            path = request.url.path
            if request.method == "POST":
                writes.append(path)
                if path.endswith("/supplements"):
                    assert "冻结发布样本" in json.loads(request.content)["items"][0]["text"]
            elif path.startswith("/api/v1/investigations/"):
                parts = path.split("/")[4:]
                if len(parts) == 1:
                    await investigation.run_once()
                elif len(parts) == 4 and parts[1:3] == ["human-wait", "preparations"]:
                    await preparation.run_once()
                elif len(parts) == 6 and parts[4] == "reassessments":
                    await review.run_once()
                elif len(parts) == 3 and parts[1] == "candidate-evaluations":
                    await candidate.run_once()
                assert not model_errors, model_errors
            response = await api.request(
                request.method,
                path,
                headers=request.headers,
                content=request.content,
            )
            return httpx2.Response(response.status_code, content=response.content)

        def request(request):
            if f"http://{request.url.host}:{request.url.port}" in {ORDER, CHANGE}:
                assert request.method == "GET" and request.url.path == "/health"
                return httpx2.Response(200, json={"status": "ok"})
            return asyncio.run_coroutine_threadsafe(forward(request), loop).result(timeout=90)

        control = SimpleNamespace(
            clock=lambda: dict(negative_steps=0, steps_over_100ms=0),
            require_frozen_worker=lambda: None,
            restart_handoff_services=lambda: asyncio.run_coroutine_threadsafe(
                replace_api(), loop
            ).result(timeout=60),
        )
        client = DemoClient(
            read_config(demo_dir)["tokens"], transport=httpx2.MockTransport(request), timeout=15
        )
        code, report = await asyncio.to_thread(
            execute, demo_dir, scenario="handoff", client=client, control=control
        )

    data = json.loads(report.read_text(encoding="utf-8"))
    assert code == 0 and data["status"] == "passed", data
    events = {item["stage"]: item for item in data["events"]}
    assert events["investigation_started"]["data_source_mode"] == "frozen"
    assert events["handoff"]["completed_rounds"] == [1, 2]
    assert events["handoff_recovered"]["budget_unchanged"] and restarts == [True]
    assert events["human_continued"]["wait_version"] == 2
    assert all("approval" not in path and "execution" not in path for path in writes)
    run_id = UUID(events["handoff"]["run_id"])
    run = await InvestigationStore(db).get_run(run_id)
    assert run.graph_version == "dynamic-investigation-graph-v15"
    routes = await ReviewRoutingQueries(db).list_for_run(run_id)
    assert [route.decision.route for route in routes] == ["follow_up", "await_human"]
    budget = await CallBudgetQueries(db).get_snapshot(run_id)
    assert budget.deadline == datetime.fromisoformat(events["handoff"]["deadline"])
    assert budget.reserved_model_calls > events["handoff"]["model_calls"]
