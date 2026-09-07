"""完整 Worker 图使用实际 SDK 和假传输验证模型预算边界。"""

import json
from datetime import timedelta

import httpx2
import pytest
from sqlalchemy import update

from opsswarm.checkpoints import initialize_checkpoint_schema, open_checkpoint_saver
from opsswarm.investigation.budget.queries import CallBudgetQueries
from opsswarm.investigation.models import (
    EvidenceKind,
    EvidenceObservation,
    InvestigationRequest,
    SpecialistReport,
)
from opsswarm.investigation.parallel_graph import LangGraphParallelInvestigator
from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.investigation.prompt_versions import MULTI_AGENT_PROMPT_VERSION
from opsswarm.investigation.workflow_versions import WorkflowVersion
from opsswarm.worker.processor import LangGraphMultiAgentProcessor
from opsswarm.worker.runner import InvestigationWorker
from opsswarm.worker.task_graph import TaskGraphFactory
from tests.checkpoint_fakes import (
    ConclusionSynthesizerStub,
    CountingDataSource,
    SpecialistAnalystStub,
)
from tests.model_budget_helpers import model_policy, sdk_model, sdk_response
from tests.task_facts_helpers import BASE_TIME, TaskClock, started_run

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.mark.parametrize(
    "outcome",
    ["success", "unknown", "synthesis_limit", "profile_mismatch", "legacy", "input_limit"],
)
async def test_graph_covers_specialists_and_synthesis_under_one_budget(
    database_session_factory, checkpoint_database_url, outcome
):
    await initialize_checkpoint_schema(checkpoint_database_url)
    policy = (
        model_policy(max_output_tokens=12288) if outcome == "synthesis_limit" else model_policy()
    )
    if outcome == "input_limit":
        policy = model_policy(max_input_tokens_per_call=1000)
    parent, started = await started_run(
        database_session_factory, workflow_version=WorkflowVersion.V2, model_budget=policy
    )
    if outcome == "legacy":
        async with database_session_factory() as session, session.begin():
            await session.execute(
                update(InvestigationRunRow)
                .where(
                    InvestigationRunRow.id == started.run.id,
                )
                .values(context_policy_version=None, prompt_version=MULTI_AGENT_PROMPT_VERSION)
            )
    clock = TaskClock(BASE_TIME + timedelta(seconds=2))
    requests = []
    queries = CallBudgetQueries(database_session_factory)

    async def reply(request):
        payload = json.loads(request.content)
        budget = await queries.get_snapshot(started.run.id)
        assert budget.reserved_model_calls >= len(requests) + 1
        requests.append(payload)
        data = json.loads(payload["messages"][-1]["content"])
        if outcome == "legacy":
            assert "task_context" not in data
        else:
            assert data["task_context"]["investigation_run_id"] == str(started.run.id)
            matching = [
                c
                for c in budget.calls
                if c.kind == "model"
                and c.status == "reserved"
                and c.operation
                == (f"analyze_{data['specialist']}" if "specialist" in data else "synthesize")
            ]
            assert len(matching) == 1
            manifest = await queries.get_context_manifest(started.run.id, matching[0].id)
            assert manifest is not None and manifest.allocation == matching[0].model_allocation
            assert manifest.role == data.get("specialist", "synthesis")
        target = InvestigationRequest.model_validate(data["incident"])
        evidence = [EvidenceObservation.model_validate(x) for x in data["evidence"]]
        if "specialist" in data:
            result = await SpecialistAnalystStub().analyze(
                specialist=EvidenceKind(data["specialist"]), request=target, evidence=evidence
            )
        else:
            result = await ConclusionSynthesizerStub().synthesize(
                request=target,
                evidence=evidence,
                reports=[SpecialistReport.model_validate(x) for x in data["reports"]],
            )
        return sdk_response(payload, result.model_dump(mode="json"), usage=outcome != "unknown")

    async with (
        httpx2.AsyncClient(transport=httpx2.MockTransport(reply)) as client,
        open_checkpoint_saver(checkpoint_database_url) as saver,
    ):
        model = sdk_model(client)
        source = CountingDataSource()
        analyst, synth = SpecialistAnalystStub(), ConclusionSynthesizerStub()
        profile = (
            policy.profile.model_copy(update={"profile_id": "changed"})
            if outcome == "profile_mismatch"
            else policy.profile
        )
        processor = LangGraphMultiAgentProcessor(
            investigator=LangGraphParallelInvestigator(
                data_source=source, analyst=analyst, synthesizer=synth, checkpointer=saver
            ),
            task_investigator_factory=TaskGraphFactory(
                session_factory=database_session_factory,
                data_source=source,
                analyst=analyst,
                synthesizer=synth,
                checkpointer=saver,
                clock=clock,
                budget_model=model,
                model_profile=profile,
            ),
            model_name="fake-budget",
        )
        worker = InvestigationWorker(
            store=parent, processor=processor, worker_id="model-budget", clock=clock
        )
        assert await worker.run_once()
        assert not await worker.run_once()
    run = await parent.get_run(started.run.id)
    budget = await queries.get_snapshot(run.id)
    assert run.status in ("succeeded", "failed"), run.last_error
    if outcome == "profile_mismatch":
        assert run.status == "failed" and requests == [] and budget.calls == ()
        assert run.last_error.error_code == "model_budget_profile_mismatch"
        return
    if outcome == "input_limit":
        assert run.status == "failed" and requests == [] and budget.reserved_model_calls == 0
        assert run.last_error.error_code == "model_input_budget_exceeded"
        return
    expected_calls = 3 if outcome == "synthesis_limit" else 4
    assert len(requests) == expected_calls and budget.reserved_model_calls == expected_calls
    assert all(r["max_completion_tokens"] == 4096 for r in requests)
    assert budget.model_usage.committed_output_tokens == 4096 * expected_calls
    if outcome == "synthesis_limit":
        assert run.status == "failed" and run.last_error.error_code == "run_output_budget_exceeded"
    else:
        assert run.status == "succeeded"
    model_calls = [r for r in budget.calls if r.kind == "model"]
    assert all(r.model_allocation is not None for r in model_calls)
    assert all(r.model_allocation is None for r in budget.calls if r.kind == "tool")
    if outcome == "unknown":
        assert (
            budget.model_usage.unknown_model_calls == 4
            and budget.model_usage.reported_cost_usd is None
        )
    else:
        assert budget.model_usage.reported_input_tokens == 80 * expected_calls
        assert budget.model_usage.unknown_model_calls == 0
    for call in model_calls:
        inputs = await queries.get_model_input(run.id, call.id)
        matching = [
            p for p in requests if p["messages"] == [m.model_dump() for m in inputs.messages]
        ]
        assert len(matching) == 1

        manifest = await queries.get_context_manifest(run.id, call.id)
        if outcome == "legacy":
            assert manifest is None
        else:
            assert manifest.allocation == call.model_allocation
