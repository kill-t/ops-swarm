"""真实 PostgreSQL 与 checkpoint 验证 v3 首轮开放假设综合。"""

import json
from collections import Counter
from datetime import timedelta
from uuid import UUID

import httpx2
import pytest

from opsswarm.checkpoints import initialize_checkpoint_schema, open_checkpoint_saver
from opsswarm.investigation.budget.contracts import CallOperation
from opsswarm.investigation.budget.queries import CallBudgetQueries
from opsswarm.investigation.models import EvidenceKind, InvestigationRunStatus, WorkItemError
from opsswarm.investigation.synthesis.persistence import OpenHypothesisStore
from opsswarm.investigation.synthesis.queries import OpenHypothesisQueries
from opsswarm.investigation.workflow_versions import WorkflowVersion
from opsswarm.worker.dynamic_graph import DynamicSynthesisGraphFactory
from tests.integration.test_dynamic_first_round_graph import (
    DynamicReplies,
    RecordingFrozenSource,
    processor_context,
    proposal_for,
)
from tests.loop_helpers import loop_policy
from tests.model_budget_helpers import sdk_model, sdk_response
from tests.task_facts_helpers import TaskClock, claimed_run

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


class SynthesisReplies:
    def __init__(self, db, roles, *, allow_synthesis: bool = True) -> None:
        self.professionals = DynamicReplies(db, proposal_for(*roles))
        self.calls = CallBudgetQueries(db)
        self.allow_synthesis = allow_synthesis
        self.synthesis_inputs = []

    async def __call__(self, request):
        payload = json.loads(request.content)
        data = json.loads(payload["messages"][1]["content"])
        if "working_summaries" not in data:
            return await self.professionals(request)
        assert self.allow_synthesis
        self.synthesis_inputs.append(data)
        run_id = UUID(data["task_context"]["investigation_run_id"])
        snapshot = await self.calls.get_snapshot(run_id)
        call = next(
            item
            for item in reversed(snapshot.calls)
            if item.operation is CallOperation.SYNTHESIZE and item.status == "reserved"
        )
        manifest = await self.calls.get_context_manifest(run_id, call.id)
        assert manifest is not None
        assert manifest.workflow_version == "v3"
        assert manifest.role == "synthesis"
        assert manifest.role_version == "dynamic-synthesis-v1"
        assert manifest.prompt_version == "open-hypothesis-synthesis-v1"
        evidence_ids = [item["evidence_id"] for item in data["evidence"]]
        return sdk_response(
            payload,
            {
                "statement": "首轮材料显示服务异常仍需独立审查",
                "confidence": 0.72,
                "supporting_evidence_ids": evidence_ids[:1],
                "counter_evidence_ids": evidence_ids[1:2],
                "unresolved_questions": ["是否存在尚未调查的竞争性解释？"],
            },
        )


async def current_v3_claim(database_session_factory):
    return await claimed_run(
        database_session_factory,
        workflow_version=WorkflowVersion.V3,
        model_budget=loop_policy(),
        supported_workflow_versions=(WorkflowVersion.V3,),
    )


def synthesis_factory(db, source, client, saver, clock):
    return DynamicSynthesisGraphFactory(
        session_factory=db,
        data_source=source,
        model=sdk_model(client),
        checkpointer=saver,
        clock=clock,
    )


@pytest.mark.parametrize(
    "roles",
    [
        (EvidenceKind.LOG,),
        (EvidenceKind.LOG, EvidenceKind.CHANGE),
        tuple(EvidenceKind),
    ],
)
async def test_dynamic_synthesis_uses_only_selected_first_round_materials(
    database_session_factory,
    checkpoint_database_url,
    roles,
) -> None:
    await initialize_checkpoint_schema(checkpoint_database_url)
    store, claim = await current_v3_claim(database_session_factory)
    source = RecordingFrozenSource(claim.request)
    replies = SynthesisReplies(database_session_factory, roles)

    async with (
        httpx2.AsyncClient(transport=httpx2.MockTransport(replies)) as client,
        open_checkpoint_saver(checkpoint_database_url) as saver,
    ):
        graph = synthesis_factory(database_session_factory, source, client, saver, TaskClock())(
            processor_context(claim)
        )
        result = await graph.investigate(claim.request, thread_id=claim.run.graph_thread_id)
        assert await graph.resume(thread_id=claim.run.graph_thread_id) == result
        checkpoint = await saver.aget_tuple(
            {"configurable": {"thread_id": claim.run.graph_thread_id}}
        )

    assert result.open_hypothesis.hypothesis.status == "pending_review"
    assert "root_cause" not in result.open_hypothesis.hypothesis.model_dump()
    assert "recommendation" not in result.open_hypothesis.hypothesis.model_dump()
    assert len(replies.synthesis_inputs) == 1
    supplied = replies.synthesis_inputs[0]
    supplied_roles = {
        item["summary"]["report"]["specialist"] for item in supplied["working_summaries"]
    }
    assert supplied_roles == {role.value for role in roles}
    assert {item["role"] for item in supplied["task_context"]["tasks"]} == supplied_roles
    stored = await OpenHypothesisQueries(database_session_factory).list_for_run(claim.run.id)
    assert stored == (result.open_hypothesis,)
    budget = await CallBudgetQueries(database_session_factory).get_snapshot(claim.run.id)
    operations = Counter(item.operation for item in budget.calls)
    assert operations[CallOperation.COORDINATE] == 1
    assert operations[CallOperation.SYNTHESIZE] == 1
    assert budget.reserved_model_calls == 2 + 2 * len(roles)
    assert budget.reserved_tool_calls == 2 * len(roles)
    run = await store.get_run(claim.run.id)
    assert run.status is InvestigationRunStatus.RUNNING and run.result is None
    assert checkpoint is not None
    assert checkpoint.metadata["graph_version"] == "dynamic-investigation-graph-v3"


async def test_dynamic_synthesis_reuses_successful_call_before_hypothesis_commit(
    database_session_factory,
    checkpoint_database_url,
    monkeypatch,
) -> None:
    await initialize_checkpoint_schema(checkpoint_database_url)
    store, claim = await current_v3_claim(database_session_factory)
    roles = (EvidenceKind.LOG, EvidenceKind.CHANGE)
    clock = TaskClock()
    source = RecordingFrozenSource(claim.request)
    replies = SynthesisReplies(database_session_factory, roles)

    async def crash_before_save(*args, **kwargs):
        raise RuntimeError("模拟综合调用提交后假设保存前中断")

    with monkeypatch.context() as patch:
        patch.setattr(OpenHypothesisStore, "save", crash_before_save)
        async with (
            httpx2.AsyncClient(transport=httpx2.MockTransport(replies)) as client,
            open_checkpoint_saver(checkpoint_database_url) as saver,
        ):
            graph = synthesis_factory(database_session_factory, source, client, saver, clock)(
                processor_context(claim)
            )
            with pytest.raises(RuntimeError, match="假设保存前中断"):
                await graph.investigate(claim.request, thread_id=claim.run.graph_thread_id)

    before = await CallBudgetQueries(database_session_factory).get_snapshot(claim.run.id)
    assert Counter(item.operation for item in before.calls)[CallOperation.SYNTHESIZE] == 1
    assert not await OpenHypothesisQueries(database_session_factory).list_for_run(claim.run.id)
    await store.fail(
        claim.work_item.id,
        lease_token=claim.work_item.lease_token,
        occurred_at=clock(),
        error=WorkItemError(error_code="crash", message="开放假设提交窗口中断"),
        retry_delay=timedelta(0),
    )
    clock.current += timedelta(seconds=1)
    replacement = await store.claim_next(
        worker_id="dynamic-synthesis-recovery",
        occurred_at=clock(),
        lease_duration=timedelta(seconds=30),
        supported_workflow_versions=(WorkflowVersion.V3,),
    )
    assert replacement is not None
    recovery_source = RecordingFrozenSource(replacement.request)
    recovery_replies = SynthesisReplies(database_session_factory, roles, allow_synthesis=False)
    async with (
        httpx2.AsyncClient(transport=httpx2.MockTransport(recovery_replies)) as client,
        open_checkpoint_saver(checkpoint_database_url) as saver,
    ):
        graph = synthesis_factory(database_session_factory, recovery_source, client, saver, clock)(
            processor_context(replacement)
        )
        result = await graph.resume(thread_id=replacement.run.graph_thread_id)

    after = await CallBudgetQueries(database_session_factory).get_snapshot(claim.run.id)
    assert after == before
    assert all(not recovery_source.calls[role] for role in EvidenceKind)
    assert recovery_replies.professionals.seen[claim.run.id] == []
    assert await OpenHypothesisQueries(database_session_factory).list_for_run(claim.run.id) == (
        result.open_hypothesis,
    )


async def test_dynamic_synthesis_reuses_hypothesis_committed_before_checkpoint(
    database_session_factory,
    checkpoint_database_url,
    monkeypatch,
) -> None:
    await initialize_checkpoint_schema(checkpoint_database_url)
    store, claim = await current_v3_claim(database_session_factory)
    roles = (EvidenceKind.LOG, EvidenceKind.METRIC)
    clock = TaskClock()
    source = RecordingFrozenSource(claim.request)
    replies = SynthesisReplies(database_session_factory, roles)
    original = OpenHypothesisStore.save
    crashed = False

    async def save_then_crash(self, *args, **kwargs):
        nonlocal crashed
        result = await original(self, *args, **kwargs)
        if not crashed:
            crashed = True
            raise RuntimeError("模拟开放假设提交后 checkpoint 前中断")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(OpenHypothesisStore, "save", save_then_crash)
        async with (
            httpx2.AsyncClient(transport=httpx2.MockTransport(replies)) as client,
            open_checkpoint_saver(checkpoint_database_url) as saver,
        ):
            graph = synthesis_factory(database_session_factory, source, client, saver, clock)(
                processor_context(claim)
            )
            with pytest.raises(RuntimeError, match="checkpoint 前中断"):
                await graph.investigate(claim.request, thread_id=claim.run.graph_thread_id)

    before = await CallBudgetQueries(database_session_factory).get_snapshot(claim.run.id)
    hypothesis = await OpenHypothesisQueries(database_session_factory).list_for_run(claim.run.id)
    assert len(hypothesis) == 1
    await store.fail(
        claim.work_item.id,
        lease_token=claim.work_item.lease_token,
        occurred_at=clock(),
        error=WorkItemError(error_code="crash", message="checkpoint 提交窗口中断"),
        retry_delay=timedelta(0),
    )
    clock.current += timedelta(seconds=1)
    replacement = await store.claim_next(
        worker_id="dynamic-hypothesis-recovery",
        occurred_at=clock(),
        lease_duration=timedelta(seconds=30),
        supported_workflow_versions=(WorkflowVersion.V3,),
    )
    assert replacement is not None
    recovery_source = RecordingFrozenSource(replacement.request)
    async with (
        httpx2.AsyncClient(
            transport=httpx2.MockTransport(
                lambda _: pytest.fail("已提交开放假设恢复不应再次调用模型")
            )
        ) as client,
        open_checkpoint_saver(checkpoint_database_url) as saver,
    ):
        graph = synthesis_factory(database_session_factory, recovery_source, client, saver, clock)(
            processor_context(replacement)
        )
        result = await graph.resume(thread_id=replacement.run.graph_thread_id)

    assert result.open_hypothesis == hypothesis[0]
    assert await CallBudgetQueries(database_session_factory).get_snapshot(claim.run.id) == before
    assert all(not recovery_source.calls[role] for role in EvidenceKind)
