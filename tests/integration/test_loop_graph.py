"""固定 v2 的真实框架循环、跨事故隔离和节点边界中断恢复。"""

import asyncio
import json
from collections import defaultdict
from datetime import timedelta
from uuid import UUID

import httpx2
import pytest

from opsswarm.checkpoints import initialize_checkpoint_schema, open_checkpoint_saver
from opsswarm.investigation.budget.queries import CallBudgetQueries
from opsswarm.investigation.budget.store import CallBudgetStore
from opsswarm.investigation.loop.contracts import EvidenceToolResult, LoopModelResult
from opsswarm.investigation.models import (
    EvidenceObservation,
    InvestigationDataSourceMode,
    InvestigationRequest,
    SpecialistReport,
)
from opsswarm.investigation.parallel_graph import LangGraphParallelInvestigator
from opsswarm.investigation.query_execution import ReadOnlyQueryExecutor
from opsswarm.investigation.scenarios import RELEASE_500_SCENARIO
from opsswarm.investigation.tasks.queries import CollaborationQueries
from opsswarm.investigation.workflow_versions import WorkflowVersion
from opsswarm.worker.processor import LangGraphMultiAgentProcessor
from opsswarm.worker.runner import InvestigationWorker
from opsswarm.worker.task_graph import TaskGraphFactory
from tests.checkpoint_fakes import (
    ConclusionSynthesizerStub,
    CountingDataSource,
    SpecialistAnalystStub,
)
from tests.live_task_helpers import LiveHttpFixture
from tests.loop_helpers import loop_policy, tool_response
from tests.model_budget_helpers import sdk_model, sdk_response
from tests.task_facts_helpers import BASE_TIME, TaskClock, started_run

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


class LoopReplies:
    def __init__(self, db):
        self.calls = CallBudgetQueries(db)
        self.seen = defaultdict(list)

    async def __call__(self, request):
        payload = json.loads(request.content)
        data = json.loads(payload["messages"][1]["content"])
        run_id = UUID(data["task_context"]["investigation_run_id"])
        role = data.get("specialist", "synthesis")
        self.seen[run_id].append(role)
        budget = await self.calls.get_snapshot(run_id)
        call = next(
            c
            for c in budget.calls
            if c.status == "reserved"
            and c.operation == ("synthesize" if role == "synthesis" else f"analyze_{role}")
        )
        manifest = await self.calls.get_context_manifest(run_id, call.id)
        assert manifest.investigation_run_id == run_id and manifest.version == "evidence-read-v1"
        assert data["incident"]["incident_id"] != str(run_id)
        if role == "synthesis":
            assert manifest.tool_names == () and len(data["reports"]) == 3
            result = await ConclusionSynthesizerStub().synthesize(
                request=InvestigationRequest.model_validate(data["incident"]),
                evidence=[EvidenceObservation.model_validate(e) for e in data["evidence"]],
                reports=[SpecialistReport.model_validate(r) for r in data["reports"]],
            )
            return sdk_response(payload, result.model_dump(mode="json"))
        assert manifest.tool_names == ("read_evidence",)
        catalog = data["evidence_catalog"]
        assert catalog["role"] == role and UUID(catalog["task_id"]) == manifest.task_id
        assert all("attributes" not in e for e in catalog["entries"])
        if len(payload["messages"]) == 2:
            entry = catalog["entries"][0]
            return tool_response(
                "read_evidence",
                {
                    "evidence_id": entry["evidence_id"],
                    "snapshot_sha256": catalog["snapshot_sha256"],
                },
                call_id=f"{role}-read",
            )
        detail = json.loads(payload["messages"][-1]["content"])
        assert UUID(detail["investigation_run_id"]) == run_id
        assert detail["task_id"] == catalog["task_id"]
        assert detail["snapshot_sha256"] == catalog["snapshot_sha256"]
        assert detail["evidence"]["kind"] == role
        return tool_response(
            "SpecialistReport",
            {
                "specialist": role,
                "summary": "完成有界调查",
                "findings": ["读取了所属专业证据"],
                "evidence_ids": [detail["evidence"]["evidence_id"]],
                "confidence": 0.8,
            },
            call_id=f"{role}-report",
        )


async def run_workers(db, checkpoint_url, parent, clock, source, reply, *, count=1, mode="frozen"):
    analyst, synth = SpecialistAnalystStub(), ConclusionSynthesizerStub()
    executor = ReadOnlyQueryExecutor()
    async with (
        httpx2.AsyncClient(transport=httpx2.MockTransport(reply)) as client,
        open_checkpoint_saver(checkpoint_url) as saver,
    ):
        processor = LangGraphMultiAgentProcessor(
            investigator=LangGraphParallelInvestigator(
                data_source=source,
                analyst=analyst,
                synthesizer=synth,
                checkpointer=saver,
                query_executor=executor,
            ),
            task_investigator_factory=TaskGraphFactory(
                session_factory=db,
                data_source=source,
                analyst=analyst,
                synthesizer=synth,
                checkpointer=saver,
                clock=clock,
                budget_model=sdk_model(client),
                model_profile=loop_policy().profile,
                query_executor=executor,
            ),
            model_name="fake-budget",
            data_source_mode=InvestigationDataSourceMode(mode),
            evaluation_scenario=RELEASE_500_SCENARIO if mode == "frozen" else None,
        )
        workers = [
            InvestigationWorker(
                store=parent,
                processor=processor,
                worker_id=f"loop-{i}",
                clock=clock,
                base_retry_delay=timedelta(0),
                max_retry_delay=timedelta(0),
                data_source_mode=InvestigationDataSourceMode(mode),
            )
            for i in range(count)
        ]
        try:
            assert all(await asyncio.gather(*(w.run_once() for w in workers)))
        finally:
            executor.close()


@pytest.mark.parametrize("mode", ["frozen", "live"])
async def test_two_incidents_complete_separate_loop_contexts(
    database_session_factory, checkpoint_database_url, mode
):
    await initialize_checkpoint_schema(checkpoint_database_url)
    parent, first = await started_run(
        database_session_factory,
        workflow_version=WorkflowVersion.V2,
        data_source_mode=InvestigationDataSourceMode(mode),
        model_budget=loop_policy(),
        context_policy_version="evidence-read-v1",
    )
    _, second = await started_run(
        database_session_factory,
        workflow_version=WorkflowVersion.V2,
        data_source_mode=InvestigationDataSourceMode(mode),
        model_budget=loop_policy(),
        context_policy_version="evidence-read-v1",
    )
    clock = TaskClock(BASE_TIME + timedelta(seconds=2))
    fixture = LiveHttpFixture(first.run.observation_cutoff_at, clock) if mode == "live" else None
    reply = LoopReplies(database_session_factory)
    await run_workers(
        database_session_factory,
        checkpoint_database_url,
        parent,
        clock,
        fixture.source if fixture else CountingDataSource(),
        reply,
        count=2,
        mode=mode,
    )
    if fixture:
        fixture.assert_windows()
    for started in (first, second):
        run = await parent.get_run(started.run.id)
        assert run.status == "succeeded", run.last_error
        assert sorted(reply.seen[run.id]) == [
            "change",
            "change",
            "log",
            "log",
            "metric",
            "metric",
            "synthesis",
        ]
        budget = await reply.calls.get_snapshot(run.id)
        assert budget.reserved_model_calls == 7 and budget.reserved_tool_calls == 6
        assert budget.model_usage.unknown_model_calls == 0


@pytest.mark.parametrize(
    "boundary", ["model_before_commit", "model_after_commit", "tool_after_commit"]
)
async def test_restart_uses_same_snapshot_new_attempt_and_shared_budget(
    database_session_factory,
    checkpoint_database_url,
    monkeypatch,
    boundary,
):
    await initialize_checkpoint_schema(checkpoint_database_url)
    parent, started = await started_run(
        database_session_factory,
        workflow_version=WorkflowVersion.V2,
        model_budget=loop_policy(),
        context_policy_version="evidence-read-v1",
    )
    clock = TaskClock(BASE_TIME + timedelta(seconds=2))
    facts = CollaborationQueries(database_session_factory)
    reply = LoopReplies(database_session_factory)
    original = CallBudgetStore.settle

    async def crash(self, run_id, reservation_id, **kwargs):
        result = kwargs.get("call_result")
        is_target = (
            isinstance(result, EvidenceToolResult)
            if boundary == "tool_after_commit"
            else isinstance(result, LoopModelResult) and len(result.messages) == 1
        )
        calls = await reply.calls.get_snapshot(run_id)
        row = next(c for c in calls.calls if c.id == reservation_id)
        if not is_target or row.operation not in ("analyze_log", "read_log_evidence"):
            return await original(self, run_id, reservation_id, **kwargs)
        if boundary != "model_before_commit":
            await original(self, run_id, reservation_id, **kwargs)
        async with asyncio.timeout(15):
            while (
                sum(
                    t.status == "succeeded"
                    for t in (await facts.get_snapshot(run_id)).rounds[0].tasks
                )
                != 2
            ):
                await asyncio.sleep(0.01)
        raise RuntimeError("模拟节点持久交互边界崩溃")

    with monkeypatch.context() as patch:
        patch.setattr(CallBudgetStore, "settle", crash)
        await run_workers(
            database_session_factory,
            checkpoint_database_url,
            parent,
            clock,
            CountingDataSource(),
            reply,
        )
    run = await parent.get_run(started.run.id)
    assert run.status == "queued", run.last_error
    before = await reply.calls.get_snapshot(run.id)
    log = next(
        t for t in (await facts.get_snapshot(run.id)).rounds[0].tasks if t.assignment.role == "log"
    )
    evidence_before = await facts.get_evidence(run.id, log.id)
    old_calls = {
        c.id: (c.status, await reply.calls.get_interaction(run.id, c.id)) for c in before.calls
    }
    clock.current += timedelta(seconds=1)
    source = CountingDataSource()
    await run_workers(
        database_session_factory, checkpoint_database_url, parent, clock, source, reply
    )
    finished = await parent.get_run(run.id)
    assert finished.status == "succeeded", finished.last_error
    assert sum(source.calls.values()) == 0
    assert await facts.get_evidence(run.id, log.id) == evidence_before
    after = await reply.calls.get_snapshot(run.id)
    assert after.reserved_model_calls == 8
    assert after.reserved_tool_calls == (7 if boundary == "tool_after_commit" else 6)
    for call in after.calls:
        if call.id in old_calls:
            assert (call.status, await reply.calls.get_interaction(run.id, call.id)) == old_calls[
                call.id
            ]
    log = next(t for t in (await facts.get_snapshot(run.id)).rounds[0].tasks if t.id == log.id)
    assert len(log.attempts) == 2 and log.attempts[0].id != log.attempts[1].id
