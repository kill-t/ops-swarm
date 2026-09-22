"""真实 PostgreSQL 与 checkpoint 验证 v3 开放假设独立审查。"""

import json
from collections import Counter
from datetime import timedelta
from uuid import UUID

import httpx2
import pytest

from opsswarm.checkpoints import initialize_checkpoint_schema, open_checkpoint_saver
from opsswarm.investigation.budget.contracts import CallOperation
from opsswarm.investigation.budget.queries import CallBudgetQueries
from opsswarm.investigation.context.prompts import HYPOTHESIS_REVIEW_PROMPT
from opsswarm.investigation.models import EvidenceKind, InvestigationRunStatus, WorkItemError
from opsswarm.investigation.review.persistence import HypothesisReviewStore
from opsswarm.investigation.review.queries import HypothesisReviewQueries
from opsswarm.investigation.workflow_versions import WorkflowVersion
from opsswarm.worker.dynamic_graph import DynamicReviewGraphFactory
from tests.integration.test_dynamic_first_round_graph import (
    DynamicReplies,
    RecordingFrozenSource,
    processor_context,
    proposal_for,
)
from tests.loop_helpers import loop_policy
from tests.model_budget_helpers import sdk_model, sdk_response
from tests.task_facts_helpers import BASE_TIME, TaskClock, started_run

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


class ReviewReplies:
    def __init__(self, db, roles) -> None:
        self.professionals = DynamicReplies(db, proposal_for(*roles))
        self.calls = CallBudgetQueries(db)
        self.synthesis_inputs = []
        self.review_inputs = []

    async def __call__(self, request):
        payload = json.loads(request.content)
        data = json.loads(payload["messages"][1]["content"])
        if "working_summaries" not in data:
            return await self.professionals(request)
        evidence_ids = [item["evidence_id"] for item in data["evidence"]]
        if "open_hypothesis" not in data:
            self.synthesis_inputs.append(data)
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

        self.review_inputs.append(data)
        assert payload["messages"][0]["content"] == HYPOTHESIS_REVIEW_PROMPT
        run_id = UUID(data["task_context"]["investigation_run_id"])
        snapshot = await self.calls.get_snapshot(run_id)
        review_call = next(
            item
            for item in reversed(snapshot.calls)
            if item.operation is CallOperation.REVIEW and item.status == "reserved"
        )
        synthesis_call = next(
            item
            for item in reversed(snapshot.calls)
            if item.operation is CallOperation.SYNTHESIZE and item.status == "succeeded"
        )
        review_manifest = await self.calls.get_context_manifest(run_id, review_call.id)
        synthesis_manifest = await self.calls.get_context_manifest(run_id, synthesis_call.id)
        assert review_manifest is not None and synthesis_manifest is not None
        assert review_manifest.workflow_version == "v3"
        assert review_manifest.role == "reviewer"
        assert review_manifest.role_version == "independent-review-v1"
        assert review_manifest.prompt_version == "open-hypothesis-review-v1"
        assert review_manifest.tool_names == ()
        assert review_manifest.materials[:-1] == synthesis_manifest.materials
        assert review_manifest.materials[-1].kind == "open_hypothesis"
        hypothesis = data["open_hypothesis"]["hypothesis"]
        return sdk_response(
            payload,
            {
                "support_is_sufficient": True,
                "rationale": "独立核对后，首轮支持证据足以交由确定性路由评估。",
                "accepted_supporting_evidence_ids": hypothesis["supporting_evidence_ids"][:1],
                "counter_evidence_ids": hypothesis["counter_evidence_ids"],
                "evidence_gaps": [],
                "competing_explanations": [],
                "suggested_direction": "evaluate_for_routing",
                "follow_up_suggestions": [],
            },
        )


async def current_v4_claim(database_session_factory):
    store, started = await started_run(
        database_session_factory,
        workflow_version=WorkflowVersion.V3,
        model_budget=loop_policy(),
    )
    assert started.run.graph_version == "dynamic-investigation-graph-v4"
    claim = await store.claim_next(
        worker_id="dynamic-review-worker",
        occurred_at=BASE_TIME + timedelta(seconds=2),
        lease_duration=timedelta(seconds=30),
        supported_workflow_versions=(WorkflowVersion.V3,),
    )
    assert claim is not None
    return store, claim


def review_factory(db, source, client, saver, clock):
    return DynamicReviewGraphFactory(
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
async def test_dynamic_review_uses_synthesis_materials_and_stays_non_terminal(
    database_session_factory,
    checkpoint_database_url,
    roles,
) -> None:
    await initialize_checkpoint_schema(checkpoint_database_url)
    store, claim = await current_v4_claim(database_session_factory)
    source = RecordingFrozenSource(claim.request)
    replies = ReviewReplies(database_session_factory, roles)

    async with (
        httpx2.AsyncClient(transport=httpx2.MockTransport(replies)) as client,
        open_checkpoint_saver(checkpoint_database_url) as saver,
    ):
        graph = review_factory(database_session_factory, source, client, saver, TaskClock())(
            processor_context(claim)
        )
        result = await graph.investigate(claim.request, thread_id=claim.run.graph_thread_id)
        assert await graph.resume(thread_id=claim.run.graph_thread_id) == result
        checkpoint = await saver.aget_tuple(
            {"configurable": {"thread_id": claim.run.graph_thread_id}}
        )

    assert len(replies.synthesis_inputs) == 1
    assert len(replies.review_inputs) == 1
    supplied = replies.review_inputs[0]
    assert supplied["open_hypothesis"] == result.open_hypothesis.model_dump(mode="json")
    assert supplied["working_summaries"] == replies.synthesis_inputs[0]["working_summaries"]
    review_dump = result.hypothesis_review.review.model_dump()
    assert result.hypothesis_review.status == "recorded"
    assert "route" not in review_dump
    assert "action" not in review_dump
    assert "approved" not in review_dump
    assert await HypothesisReviewQueries(database_session_factory).list_for_run(claim.run.id) == (
        result.hypothesis_review,
    )
    budget = await CallBudgetQueries(database_session_factory).get_snapshot(claim.run.id)
    operations = Counter(item.operation for item in budget.calls)
    assert operations[CallOperation.REVIEW] == 1
    assert budget.reserved_model_calls == 3 + 2 * len(roles)
    assert budget.reserved_tool_calls == 2 * len(roles)
    run = await store.get_run(claim.run.id)
    assert run.status is InvestigationRunStatus.RUNNING and run.result is None
    assert checkpoint is not None
    assert checkpoint.metadata["graph_version"] == "dynamic-investigation-graph-v4"


async def test_dynamic_review_reuses_successful_call_before_review_commit(
    database_session_factory,
    checkpoint_database_url,
    monkeypatch,
) -> None:
    await initialize_checkpoint_schema(checkpoint_database_url)
    store, claim = await current_v4_claim(database_session_factory)
    roles = (EvidenceKind.LOG, EvidenceKind.CHANGE)
    clock = TaskClock()
    replies = ReviewReplies(database_session_factory, roles)

    async def crash_before_save(*args, **kwargs):
        raise RuntimeError("模拟审查调用提交后事实保存前中断")

    with monkeypatch.context() as patch:
        patch.setattr(HypothesisReviewStore, "save", crash_before_save)
        async with (
            httpx2.AsyncClient(transport=httpx2.MockTransport(replies)) as client,
            open_checkpoint_saver(checkpoint_database_url) as saver,
        ):
            graph = review_factory(
                database_session_factory,
                RecordingFrozenSource(claim.request),
                client,
                saver,
                clock,
            )(processor_context(claim))
            with pytest.raises(RuntimeError, match="事实保存前中断"):
                await graph.investigate(claim.request, thread_id=claim.run.graph_thread_id)

    before = await CallBudgetQueries(database_session_factory).get_snapshot(claim.run.id)
    assert Counter(item.operation for item in before.calls)[CallOperation.REVIEW] == 1
    assert not await HypothesisReviewQueries(database_session_factory).list_for_run(claim.run.id)
    await store.fail(
        claim.work_item.id,
        lease_token=claim.work_item.lease_token,
        occurred_at=clock(),
        error=WorkItemError(error_code="crash", message="独立审查提交窗口中断"),
        retry_delay=timedelta(0),
    )
    clock.current += timedelta(seconds=1)
    replacement = await store.claim_next(
        worker_id="dynamic-review-recovery",
        occurred_at=clock(),
        lease_duration=timedelta(seconds=30),
        supported_workflow_versions=(WorkflowVersion.V3,),
    )
    assert replacement is not None
    recovery_source = RecordingFrozenSource(replacement.request)
    async with (
        httpx2.AsyncClient(
            transport=httpx2.MockTransport(
                lambda _: pytest.fail("成功审查调用恢复不应再次调用模型")
            )
        ) as client,
        open_checkpoint_saver(checkpoint_database_url) as saver,
    ):
        graph = review_factory(database_session_factory, recovery_source, client, saver, clock)(
            processor_context(replacement)
        )
        result = await graph.resume(thread_id=replacement.run.graph_thread_id)

    assert await CallBudgetQueries(database_session_factory).get_snapshot(claim.run.id) == before
    assert all(not recovery_source.calls[role] for role in EvidenceKind)
    assert await HypothesisReviewQueries(database_session_factory).list_for_run(claim.run.id) == (
        result.hypothesis_review,
    )


async def test_dynamic_review_reuses_review_committed_before_checkpoint(
    database_session_factory,
    checkpoint_database_url,
    monkeypatch,
) -> None:
    await initialize_checkpoint_schema(checkpoint_database_url)
    store, claim = await current_v4_claim(database_session_factory)
    roles = (EvidenceKind.LOG, EvidenceKind.METRIC)
    clock = TaskClock()
    replies = ReviewReplies(database_session_factory, roles)
    original = HypothesisReviewStore.save
    crashed = False

    async def save_then_crash(self, *args, **kwargs):
        nonlocal crashed
        result = await original(self, *args, **kwargs)
        if not crashed:
            crashed = True
            raise RuntimeError("模拟审查事实提交后 checkpoint 前中断")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(HypothesisReviewStore, "save", save_then_crash)
        async with (
            httpx2.AsyncClient(transport=httpx2.MockTransport(replies)) as client,
            open_checkpoint_saver(checkpoint_database_url) as saver,
        ):
            graph = review_factory(
                database_session_factory,
                RecordingFrozenSource(claim.request),
                client,
                saver,
                clock,
            )(processor_context(claim))
            with pytest.raises(RuntimeError, match="checkpoint 前中断"):
                await graph.investigate(claim.request, thread_id=claim.run.graph_thread_id)

    before = await CallBudgetQueries(database_session_factory).get_snapshot(claim.run.id)
    reviews = await HypothesisReviewQueries(database_session_factory).list_for_run(claim.run.id)
    assert len(reviews) == 1
    await store.fail(
        claim.work_item.id,
        lease_token=claim.work_item.lease_token,
        occurred_at=clock(),
        error=WorkItemError(error_code="crash", message="checkpoint 提交窗口中断"),
        retry_delay=timedelta(0),
    )
    clock.current += timedelta(seconds=1)
    replacement = await store.claim_next(
        worker_id="dynamic-review-fact-recovery",
        occurred_at=clock(),
        lease_duration=timedelta(seconds=30),
        supported_workflow_versions=(WorkflowVersion.V3,),
    )
    assert replacement is not None
    recovery_source = RecordingFrozenSource(replacement.request)
    async with (
        httpx2.AsyncClient(
            transport=httpx2.MockTransport(
                lambda _: pytest.fail("已提交独立审查恢复不应再次调用模型")
            )
        ) as client,
        open_checkpoint_saver(checkpoint_database_url) as saver,
    ):
        graph = review_factory(database_session_factory, recovery_source, client, saver, clock)(
            processor_context(replacement)
        )
        result = await graph.resume(thread_id=replacement.run.graph_thread_id)

    assert result.hypothesis_review == reviews[0]
    assert await CallBudgetQueries(database_session_factory).get_snapshot(claim.run.id) == before
    assert all(not recovery_source.calls[role] for role in EvidenceKind)
