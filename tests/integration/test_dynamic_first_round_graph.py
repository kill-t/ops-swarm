"""真实 PostgreSQL 与 checkpoint 验证 v3 动态首轮专业分派。"""

import asyncio
import json
from collections import Counter, defaultdict
from datetime import timedelta
from uuid import UUID

import httpx2
import pytest
from langchain_openai.chat_models.base import OpenAIConnectionError

from opsswarm.checkpoints import initialize_checkpoint_schema, open_checkpoint_saver
from opsswarm.investigation.budget.contracts import CallOperation
from opsswarm.investigation.budget.queries import CallBudgetQueries
from opsswarm.investigation.coordination.contracts import (
    CoordinationProposal,
    ProposedTask,
)
from opsswarm.investigation.models import (
    EvidenceKind,
    InvestigationRunStatus,
    WorkItemError,
)
from opsswarm.investigation.scenarios import RELEASE_500_SCENARIO, FrozenScenarioDataSource
from opsswarm.investigation.tasks.queries import CollaborationQueries
from opsswarm.investigation.tasks.store import InvestigationTaskStore
from opsswarm.investigation.workflow_versions import WorkflowVersion
from opsswarm.worker.dynamic_graph import DynamicFirstRoundGraphFactory
from opsswarm.worker.runner import ProcessorContext
from tests.loop_helpers import loop_policy, tool_response
from tests.model_budget_helpers import sdk_model, sdk_response
from tests.task_facts_helpers import BASE_TIME, TaskClock, claimed_run

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_LOOKBACK_SECONDS = {
    EvidenceKind.LOG: 600,
    EvidenceKind.METRIC: 300,
    EvidenceKind.CHANGE: 3600,
}


def proposal_for(*roles: EvidenceKind) -> CoordinationProposal:
    return CoordinationProposal(
        round_number=1,
        goal="核对首轮最相关的专业材料",
        tasks=tuple(
            ProposedTask(
                task_key=role.value,
                role=role,
                goal=f"分析 {role.value} 专业材料",
                lookback_seconds=_LOOKBACK_SECONDS[role],
            )
            for role in roles
        ),
    )


class RecordingFrozenSource(FrozenScenarioDataSource):
    def __init__(self, request) -> None:
        current = tuple(
            item.model_copy(update={"observed_at": BASE_TIME})
            for item in RELEASE_500_SCENARIO.evidence
        )
        stale_log = next(item for item in current if item.kind is EvidenceKind.LOG).model_copy(
            update={
                "evidence_id": "log-outside-dynamic-window",
                "observed_at": BASE_TIME - timedelta(hours=1),
            }
        )
        super().__init__(
            RELEASE_500_SCENARIO.model_copy(
                update={
                    "request": request,
                    "evidence": (*current, stale_log),
                }
            )
        )
        self.calls = {role: [] for role in EvidenceKind}

    def query_logs(self, *, service_name: str, lookback_minutes: int):
        self.calls[EvidenceKind.LOG].append((service_name, lookback_minutes))
        return super().query_logs(service_name=service_name, lookback_minutes=lookback_minutes)

    def query_metrics(self, *, service_name: str, lookback_minutes: int):
        self.calls[EvidenceKind.METRIC].append((service_name, lookback_minutes))
        return super().query_metrics(service_name=service_name, lookback_minutes=lookback_minutes)

    def list_recent_changes(self, *, service_name: str, lookback_hours: int):
        self.calls[EvidenceKind.CHANGE].append((service_name, lookback_hours))
        return super().list_recent_changes(service_name=service_name, lookback_hours=lookback_hours)


def processor_context(claim) -> ProcessorContext:
    return ProcessorContext(
        request=claim.request,
        mode=claim.run.mode,
        data_source_mode=claim.run.data_source_mode,
        work_item_kind=claim.work_item.kind,
        graph_thread_id=claim.run.graph_thread_id,
        workflow_version=claim.run.workflow_version,
        graph_version=claim.run.graph_version,
        coordination_policy=claim.run.coordination_policy,
        investigation_run_id=claim.run.id,
        lease_token=claim.work_item.lease_token,
        call_budget=claim.run.call_budget,
        model_budget=claim.run.model_budget,
        context_policy_version=claim.run.context_policy_version,
        evidence_policy_version=claim.run.evidence_policy_version,
        observation_cutoff_at=claim.run.observation_cutoff_at,
    )


async def current_v3_claim(database_session_factory):
    return await claimed_run(
        database_session_factory,
        workflow_version=WorkflowVersion.V3,
        model_budget=loop_policy(),
        supported_workflow_versions=(WorkflowVersion.V3,),
    )


class DynamicReplies:
    def __init__(
        self,
        db,
        proposed: CoordinationProposal,
        *,
        fail_first_log: bool = False,
        allow_coordination: bool = True,
    ) -> None:
        self.calls = CallBudgetQueries(db)
        self.facts = CollaborationQueries(db)
        self.proposed = proposed
        self.fail_first_log = fail_first_log
        self.allow_coordination = allow_coordination
        self.seen = defaultdict(list)

    async def __call__(self, request):
        payload = json.loads(request.content)
        data = json.loads(payload["messages"][1]["content"])
        if "round_number" in data and "policy" in data:
            assert self.allow_coordination
            return sdk_response(payload, self.proposed.model_dump(mode="json"))
        role = EvidenceKind(data["specialist"])
        run_id = UUID(data["task_context"]["investigation_run_id"])
        self.seen[run_id].append(role)
        snapshot = await self.calls.get_snapshot(run_id)
        call = next(
            item
            for item in snapshot.calls
            if item.status == "reserved"
            and item.operation == CallOperation(f"analyze_{role.value}")
        )
        manifest = await self.calls.get_context_manifest(run_id, call.id)
        assert manifest is not None and manifest.workflow_version == "v3"
        if self.fail_first_log and role is EvidenceKind.LOG and len(payload["messages"]) == 2:
            selected_companions = {item.role for item in self.proposed.tasks} - {role}
            async with asyncio.timeout(15):
                while True:
                    facts = await self.facts.get_snapshot(run_id)
                    succeeded = {
                        task.assignment.role
                        for task in facts.rounds[0].tasks
                        if task.status == "succeeded"
                    }
                    if selected_companions <= succeeded:
                        break
                    await asyncio.sleep(0.01)
            self.fail_first_log = False
            raise RuntimeError("模拟日志专业模型中断")
        catalog = data["evidence_catalog"]
        if len(payload["messages"]) == 2:
            entry = catalog["entries"][0]
            return tool_response(
                "read_evidence",
                {
                    "evidence_id": entry["evidence_id"],
                    "snapshot_sha256": catalog["snapshot_sha256"],
                },
                call_id=f"{role.value}-read",
            )
        detail = json.loads(payload["messages"][-1]["content"])
        return tool_response(
            "SpecialistReport",
            {
                "specialist": role.value,
                "summary": f"{role.value} 动态任务完成",
                "findings": ["读取了所属专业证据"],
                "evidence_ids": [detail["evidence"]["evidence_id"]],
                "confidence": 0.8,
            },
            call_id=f"{role.value}-report",
        )


@pytest.mark.parametrize(
    "roles",
    [
        (EvidenceKind.LOG,),
        (EvidenceKind.LOG, EvidenceKind.CHANGE),
        tuple(EvidenceKind),
    ],
)
async def test_dynamic_first_round_runs_only_selected_tasks(
    database_session_factory,
    checkpoint_database_url,
    roles,
) -> None:
    await initialize_checkpoint_schema(checkpoint_database_url)
    store, claim = await current_v3_claim(database_session_factory)
    source = RecordingFrozenSource(claim.request)
    replies = DynamicReplies(database_session_factory, proposal_for(*roles))

    async with (
        httpx2.AsyncClient(transport=httpx2.MockTransport(replies)) as client,
        open_checkpoint_saver(checkpoint_database_url) as saver,
    ):
        graph = DynamicFirstRoundGraphFactory(
            session_factory=database_session_factory,
            data_source=source,
            model=sdk_model(client),
            checkpointer=saver,
            clock=TaskClock(),
        )(processor_context(claim))
        result = await graph.investigate(claim.request, thread_id=claim.run.graph_thread_id)
        assert await graph.resume(thread_id=claim.run.graph_thread_id) == result
        checkpoint = await saver.aget_tuple(
            {"configurable": {"thread_id": claim.run.graph_thread_id}}
        )

    selected = set(roles)
    assert {item.report.specialist for item in result.specialist_results} == selected
    facts = CollaborationQueries(database_session_factory)
    collaboration = await facts.get_snapshot(claim.run.id)
    assert len(collaboration.rounds) == 1
    assert collaboration.rounds[0].status == "completed"
    assert {task.assignment.role for task in collaboration.rounds[0].tasks} == selected
    for task in collaboration.rounds[0].tasks:
        assert task.status == "succeeded" and task.attempt_count == 1
        stored = await facts.get_result(claim.run.id, task.id)
        evidence = await facts.get_evidence(claim.run.id, task.id)
        summary = await facts.get_working_summary(claim.run.id, task.id)
        assert stored is not None and evidence is not None and summary is not None
        assert stored.attempt_id == summary.attempt_id == task.attempts[0].id
        assert stored.result.evidence == evidence.snapshot.evidence
        assert stored.result.report.specialist is task.assignment.role
        assert all(
            task.assignment.window_start <= item.observed_at <= task.assignment.window_end
            for item in stored.result.evidence
        )
    expected_window = {
        EvidenceKind.LOG: 10,
        EvidenceKind.METRIC: 5,
        EvidenceKind.CHANGE: 1,
    }
    for role in EvidenceKind:
        assert source.calls[role] == (
            [(claim.request.service_name, expected_window[role])] if role in selected else []
        )
        assert len(replies.seen[claim.run.id]) == 2 * len(selected)
        assert replies.seen[claim.run.id].count(role) == (2 if role in selected else 0)
    budget = await CallBudgetQueries(database_session_factory).get_snapshot(claim.run.id)
    operations = Counter(item.operation for item in budget.calls)
    assert budget.reserved_model_calls == 1 + 2 * len(selected)
    assert budget.reserved_tool_calls == 2 * len(selected)
    assert operations[CallOperation.COORDINATE] == 1
    for role in EvidenceKind:
        expected = 1 if role in selected else 0
        query_operation = (
            CallOperation(f"query_{role.value}s")
            if role is not EvidenceKind.CHANGE
            else CallOperation.LIST_CHANGES
        )
        assert operations[query_operation] == expected
        assert operations[CallOperation(f"analyze_{role.value}")] == 2 * expected
        assert operations[CallOperation(f"read_{role.value}_evidence")] == expected
    run = await store.get_run(claim.run.id)
    assert run.status is InvestigationRunStatus.RUNNING and run.result is None
    assert checkpoint is not None
    assert checkpoint.metadata["graph_version"] == "dynamic-investigation-graph-v2"
    assert len(checkpoint.checkpoint["channel_values"]["specialist_results"]) == len(selected)


async def test_dynamic_first_round_recovers_only_failed_parallel_task(
    database_session_factory,
    checkpoint_database_url,
) -> None:
    await initialize_checkpoint_schema(checkpoint_database_url)
    store, claim = await current_v3_claim(database_session_factory)
    roles = tuple(EvidenceKind)
    proposed = proposal_for(*roles)
    source = RecordingFrozenSource(claim.request)
    first_replies = DynamicReplies(database_session_factory, proposed, fail_first_log=True)
    clock = TaskClock()

    async with (
        httpx2.AsyncClient(transport=httpx2.MockTransport(first_replies)) as client,
        open_checkpoint_saver(checkpoint_database_url) as saver,
    ):
        graph = DynamicFirstRoundGraphFactory(
            session_factory=database_session_factory,
            data_source=source,
            model=sdk_model(client),
            checkpointer=saver,
            clock=clock,
        )(processor_context(claim))
        with pytest.raises(OpenAIConnectionError):
            await graph.investigate(claim.request, thread_id=claim.run.graph_thread_id)
        assert not first_replies.fail_first_log

    assert all(len(source.calls[role]) == 1 for role in EvidenceKind)
    facts = CollaborationQueries(database_session_factory)
    before = (await facts.get_snapshot(claim.run.id)).rounds[0]
    log = next(task for task in before.tasks if task.assignment.role is EvidenceKind.LOG)
    assert log.status == "pending" and len(log.attempts) == 1
    assert log.attempts[0].status == "failed"
    assert {task.assignment.role for task in before.tasks if task.status == "succeeded"} == {
        EvidenceKind.METRIC,
        EvidenceKind.CHANGE,
    }

    await store.fail(
        claim.work_item.id,
        lease_token=claim.work_item.lease_token,
        occurred_at=clock(),
        error=WorkItemError(error_code="crash", message="动态专业节点中断"),
        retry_delay=timedelta(0),
    )
    clock.current += timedelta(seconds=1)
    replacement = await store.claim_next(
        worker_id="replacement-dynamic-worker",
        occurred_at=clock(),
        lease_duration=timedelta(seconds=30),
        supported_workflow_versions=(WorkflowVersion.V3,),
    )
    assert replacement is not None
    recovery_source = RecordingFrozenSource(replacement.request)
    recovery_replies = DynamicReplies(database_session_factory, proposed, allow_coordination=False)
    async with (
        httpx2.AsyncClient(transport=httpx2.MockTransport(recovery_replies)) as client,
        open_checkpoint_saver(checkpoint_database_url) as saver,
    ):
        graph = DynamicFirstRoundGraphFactory(
            session_factory=database_session_factory,
            data_source=recovery_source,
            model=sdk_model(client),
            checkpointer=saver,
            clock=clock,
        )(processor_context(replacement))
        result = await graph.resume(thread_id=replacement.run.graph_thread_id)

    assert {item.report.specialist for item in result.specialist_results} == set(roles)
    assert all(not recovery_source.calls[role] for role in EvidenceKind)
    assert recovery_replies.seen[claim.run.id] == [EvidenceKind.LOG, EvidenceKind.LOG]
    after = (await facts.get_snapshot(claim.run.id)).rounds[0]
    assert after.status == "completed"
    assert {task.assignment.role: len(task.attempts) for task in after.tasks} == {
        EvidenceKind.LOG: 2,
        EvidenceKind.METRIC: 1,
        EvidenceKind.CHANGE: 1,
    }
    budget = await CallBudgetQueries(database_session_factory).get_snapshot(claim.run.id)
    assert budget.reserved_model_calls == 8
    assert budget.reserved_tool_calls == 6
    operations = Counter(item.operation for item in budget.calls)
    assert operations[CallOperation.COORDINATE] == 1
    events = await facts.list_events(claim.run.id)
    event_types = Counter(item.event_type for item in events)
    assert event_types["task.succeeded"] == 3
    assert event_types["task.failed"] == 1
    assert event_types["round.completed"] == 1
    assert [item.cursor for item in events] == list(range(1, len(events) + 1))


async def test_dynamic_first_round_reuses_result_committed_before_node_checkpoint(
    database_session_factory,
    checkpoint_database_url,
    monkeypatch,
) -> None:
    await initialize_checkpoint_schema(checkpoint_database_url)
    store, claim = await current_v3_claim(database_session_factory)
    roles = (EvidenceKind.LOG, EvidenceKind.CHANGE)
    proposed = proposal_for(*roles)
    source = RecordingFrozenSource(claim.request)
    replies = DynamicReplies(database_session_factory, proposed)
    facts = CollaborationQueries(database_session_factory)
    clock = TaskClock()
    original = InvestigationTaskStore.finish_attempt
    crashed = False

    async def finish_then_crash(self, run_id, task_id, attempt_id, **kwargs):
        nonlocal crashed
        record = await original(self, run_id, task_id, attempt_id, **kwargs)
        result = kwargs.get("result")
        if result is not None and result.report.specialist is EvidenceKind.LOG and not crashed:
            crashed = True
            async with asyncio.timeout(15):
                while True:
                    snapshot = await facts.get_snapshot(run_id)
                    companion = next(
                        task
                        for task in snapshot.rounds[0].tasks
                        if task.assignment.role is EvidenceKind.CHANGE
                    )
                    if companion.status == "succeeded":
                        break
                    await asyncio.sleep(0.01)
            raise RuntimeError("模拟专业结果提交后 checkpoint 前中断")
        return record

    with monkeypatch.context() as patch:
        patch.setattr(InvestigationTaskStore, "finish_attempt", finish_then_crash)
        async with (
            httpx2.AsyncClient(transport=httpx2.MockTransport(replies)) as client,
            open_checkpoint_saver(checkpoint_database_url) as saver,
        ):
            graph = DynamicFirstRoundGraphFactory(
                session_factory=database_session_factory,
                data_source=source,
                model=sdk_model(client),
                checkpointer=saver,
                clock=clock,
            )(processor_context(claim))
            with pytest.raises(RuntimeError, match="checkpoint 前中断"):
                await graph.investigate(claim.request, thread_id=claim.run.graph_thread_id)

    before_tasks = (await facts.get_snapshot(claim.run.id)).rounds[0].tasks
    before_results = {
        task.id: await facts.get_result(claim.run.id, task.id) for task in before_tasks
    }
    before_summaries = {
        task.id: await facts.get_working_summary(claim.run.id, task.id) for task in before_tasks
    }
    before_budget = await CallBudgetQueries(database_session_factory).get_snapshot(claim.run.id)
    assert all(task.status == "succeeded" for task in before_tasks)

    await store.fail(
        claim.work_item.id,
        lease_token=claim.work_item.lease_token,
        occurred_at=clock(),
        error=WorkItemError(error_code="crash", message="checkpoint 提交窗口中断"),
        retry_delay=timedelta(0),
    )
    clock.current += timedelta(seconds=1)
    replacement = await store.claim_next(
        worker_id="replacement-after-result",
        occurred_at=clock(),
        lease_duration=timedelta(seconds=30),
        supported_workflow_versions=(WorkflowVersion.V3,),
    )
    assert replacement is not None
    recovery_source = RecordingFrozenSource(replacement.request)

    async with (
        httpx2.AsyncClient(
            transport=httpx2.MockTransport(
                lambda _: pytest.fail("已提交专业结果恢复不应再次调用模型")
            )
        ) as client,
        open_checkpoint_saver(checkpoint_database_url) as saver,
    ):
        graph = DynamicFirstRoundGraphFactory(
            session_factory=database_session_factory,
            data_source=recovery_source,
            model=sdk_model(client),
            checkpointer=saver,
            clock=clock,
        )(processor_context(replacement))
        await graph.resume(thread_id=replacement.run.graph_thread_id)

    after_tasks = (await facts.get_snapshot(claim.run.id)).rounds[0].tasks
    assert all(not recovery_source.calls[role] for role in EvidenceKind)
    assert all(task.attempt_count == 1 for task in after_tasks)
    assert {
        task.id: await facts.get_result(claim.run.id, task.id) for task in after_tasks
    } == before_results
    assert {
        task.id: await facts.get_working_summary(claim.run.id, task.id) for task in after_tasks
    } == before_summaries
    after_budget = await CallBudgetQueries(database_session_factory).get_snapshot(claim.run.id)
    assert after_budget == before_budget
    events = await facts.list_events(claim.run.id)
    assert Counter(item.event_type for item in events)["task.succeeded"] == len(roles)
