"""两个事故并发执行真实图，模型请求前必须存在各自的上下文清单。"""

import asyncio
import json
from collections import defaultdict
from datetime import timedelta
from uuid import UUID

import httpx2
import pytest

from opsswarm.checkpoints import initialize_checkpoint_schema, open_checkpoint_saver
from opsswarm.investigation.budget.queries import CallBudgetQueries
from opsswarm.investigation.context.builder import content_hash
from opsswarm.investigation.context.evidence_contracts import EvidenceReadRequest
from opsswarm.investigation.context.evidence_reader import ContextEvidenceReader
from opsswarm.investigation.models import (
    EvidenceKind,
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
from tests.model_budget_helpers import model_policy, sdk_model, sdk_response
from tests.task_facts_helpers import BASE_TIME, TaskClock, started_run

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.mark.parametrize(
    "mode", [InvestigationDataSourceMode.FROZEN, InvestigationDataSourceMode.LIVE]
)
async def test_parallel_incidents_use_separate_committed_contexts(
    database_session_factory,
    checkpoint_database_url,
    mode,
):
    await initialize_checkpoint_schema(checkpoint_database_url)
    parent, first = await started_run(
        database_session_factory,
        workflow_version=WorkflowVersion.V2,
        model_budget=model_policy(),
        data_source_mode=mode,
    )
    _, second = await started_run(
        database_session_factory,
        workflow_version=WorkflowVersion.V2,
        model_budget=model_policy(),
        data_source_mode=mode,
    )
    incidents = {r.run.id: r.run.incident_id for r in (first, second)}
    budget = CallBudgetQueries(database_session_factory)
    facts = CollaborationQueries(database_session_factory)
    seen = defaultdict(list)
    analyst, synthesizer = SpecialistAnalystStub(), ConclusionSynthesizerStub()

    async def reply(request):
        payload = json.loads(request.content)
        data = json.loads(payload["messages"][-1]["content"])
        run_id = UUID(data["task_context"]["investigation_run_id"])
        assert UUID(data["incident"]["incident_id"]) == incidents[run_id]
        role = data.get("specialist", "synthesis")
        seen[run_id].append(role)
        snapshot = await budget.get_snapshot(run_id)
        call = next(
            c
            for c in snapshot.calls
            if c.status == "reserved"
            and c.kind == "model"
            and c.operation == (f"analyze_{role}" if role != "synthesis" else "synthesize")
        )
        manifest = await budget.get_context_manifest(run_id, call.id)
        assert manifest is not None and manifest.role == role
        actual = await budget.get_model_input(run_id, call.id)
        assert payload["messages"] == [m.model_dump() for m in actual.messages]
        tasks = (await facts.get_snapshot(run_id)).rounds[0].tasks
        expected_ids = {t.id for t in tasks if role == "synthesis" or t.assignment.role == role}
        assert {UUID(t["task_id"]) for t in data["task_context"]["tasks"]} == expected_ids
        assert {
            m.material_id for m in manifest.materials if m.kind != "incident_input"
        } == expected_ids
        for material in manifest.materials:
            if material.kind == "task_evidence":
                record = await facts.get_evidence(run_id, material.material_id)
                assert record.content_sha256 == material.content_sha256
            elif material.kind == "task_result":
                task = next(t for t in tasks if t.id == material.material_id)
                assert task.status == "succeeded"
                record = await facts.get_result(run_id, material.material_id)
                assert content_hash(record.result) == material.content_sha256
        reader = ContextEvidenceReader(run_id=run_id, call_id=call.id, calls=budget, tasks=facts)
        catalog = await reader.catalog()
        assert {g.task_id for g in catalog.groups} == expected_ids
        assert all(g.data_source_mode == mode for g in catalog.groups)
        returned = []
        for group in catalog.groups:
            for entry in group.entries:
                detail = await reader.read(
                    EvidenceReadRequest(
                        task_id=group.task_id,
                        evidence_id=entry.evidence_id,
                        snapshot_sha256=group.snapshot_sha256,
                    )
                )
                assert detail.evidence.summary == entry.summary
                returned.append(detail.evidence.model_dump(mode="json"))
        assert sorted(returned, key=lambda e: e["evidence_id"]) == sorted(
            data["evidence"], key=lambda e: e["evidence_id"]
        )
        target = InvestigationRequest.model_validate(data["incident"])
        evidence = [EvidenceObservation.model_validate(e) for e in data["evidence"]]
        if role == "synthesis":
            result = await synthesizer.synthesize(
                request=target,
                evidence=evidence,
                reports=[SpecialistReport.model_validate(r) for r in data["reports"]],
            )
        else:
            assert all(e.kind == role for e in evidence) and "reports" not in data
            result = await analyst.analyze(
                specialist=EvidenceKind(role), request=target, evidence=evidence
            )
        return sdk_response(payload, result.model_dump(mode="json"))

    clock = TaskClock(BASE_TIME + timedelta(seconds=2))
    async with (
        httpx2.AsyncClient(transport=httpx2.MockTransport(reply)) as client,
        open_checkpoint_saver(checkpoint_database_url) as saver,
    ):
        fixture = (
            LiveHttpFixture(first.run.observation_cutoff_at, clock) if mode == "live" else None
        )
        source = fixture.source if fixture else CountingDataSource()
        executor = ReadOnlyQueryExecutor()
        processor = LangGraphMultiAgentProcessor(
            investigator=LangGraphParallelInvestigator(
                data_source=source,
                analyst=analyst,
                synthesizer=synthesizer,
                checkpointer=saver,
                query_executor=executor,
            ),
            task_investigator_factory=TaskGraphFactory(
                session_factory=database_session_factory,
                data_source=source,
                analyst=analyst,
                synthesizer=synthesizer,
                checkpointer=saver,
                clock=clock,
                budget_model=sdk_model(client),
                model_profile=model_policy().profile,
                query_executor=executor,
            ),
            model_name="fake-budget",
            data_source_mode=mode,
            evaluation_scenario=RELEASE_500_SCENARIO if mode == "frozen" else None,
        )
        workers = [
            InvestigationWorker(
                store=parent,
                processor=processor,
                worker_id=f"context-{i}",
                clock=clock,
                data_source_mode=mode,
            )
            for i in range(2)
        ]
        try:
            assert all(await asyncio.gather(*(w.run_once() for w in workers)))
        finally:
            executor.close()
        if fixture:
            fixture.assert_windows()
    for run_id in incidents:
        run = await parent.get_run(run_id)
        assert run.status == "succeeded", run.last_error
        assert (run.metrics is None) == (mode == "live")
        assert sorted(seen[run_id]) == ["change", "log", "metric", "synthesis"]
        calls = await budget.get_snapshot(run_id)
        assert calls.reserved_model_calls == 4 and calls.reserved_tool_calls == 3

        # 已完成调用的目录仍按原材料读取，不需要有效 Worker 租约。
        for call in calls.calls:
            reader = ContextEvidenceReader(
                run_id=run_id, call_id=call.id, calls=budget, tasks=facts
            )
            catalog = await reader.catalog()
            if call.kind == "tool":
                assert catalog is None
            else:
                assert len(catalog.groups) == (3 if call.operation == "synthesize" else 1)
