"""真实 PostgreSQL 与 checkpoint 验证第二轮独立审查、来源隔离与恢复。"""

import json
from collections import Counter
from datetime import timedelta

import httpx2
import pytest
from sqlalchemy import update

from opsswarm.checkpoints import initialize_checkpoint_schema, open_checkpoint_saver
from opsswarm.investigation.budget.contracts import CallOperation
from opsswarm.investigation.budget.queries import CallBudgetQueries
from opsswarm.investigation.budget.store import CallBudgetStore
from opsswarm.investigation.context.prompts import (
    FOLLOW_UP_HYPOTHESIS_REVIEW_PROMPT,
    FOLLOW_UP_OPEN_HYPOTHESIS_PROMPT,
    HYPOTHESIS_REVIEW_PROMPT,
)
from opsswarm.investigation.models import EvidenceKind, InvestigationRunStatus, WorkItemError
from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.investigation.review.contracts import ReviewModelResult
from opsswarm.investigation.review.persistence import HypothesisReviewStore
from opsswarm.investigation.review.queries import HypothesisReviewQueries
from opsswarm.investigation.review_routing.service import SecondRoundReviewRoutingService
from opsswarm.investigation.synthesis.queries import OpenHypothesisQueries
from opsswarm.investigation.tasks.errors import TaskFactConflict
from opsswarm.investigation.tasks.queries import CollaborationQueries
from opsswarm.investigation.workflow_versions import WorkflowVersion
from opsswarm.worker.dynamic_graph import DynamicSecondRoundReviewGraphFactory
from tests.integration.test_dynamic_first_round_graph import (
    RecordingFrozenSource,
    processor_context,
)
from tests.integration.test_dynamic_review_graph import ReviewReplies
from tests.loop_helpers import loop_policy
from tests.model_budget_helpers import sdk_model
from tests.task_facts_helpers import BASE_TIME, TaskClock, started_run

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


class SecondRoundReviewReplies(ReviewReplies):
    def __init__(self, db, roles, *, route_case: str) -> None:
        super().__init__(
            db,
            roles,
            route_case=route_case,
            allow_follow_up_review=True,
        )
        self.synthesis_prompts: list[str] = []
        self.review_prompts: list[str] = []

    async def __call__(self, request):
        payload = json.loads(request.content)
        data = json.loads(payload["messages"][1]["content"])
        if "working_summaries" in data and "open_hypothesis" not in data:
            self.synthesis_prompts.append(payload["messages"][0]["content"])
        if "open_hypothesis" in data:
            self.review_prompts.append(payload["messages"][0]["content"])
        return await super().__call__(request)


async def current_v9_claim(database_session_factory):
    store, started = await started_run(
        database_session_factory,
        workflow_version=WorkflowVersion.V3,
        model_budget=loop_policy(),
    )
    async with database_session_factory() as session, session.begin():
        await session.execute(
            update(InvestigationRunRow)
            .where(InvestigationRunRow.id == started.run.id)
            .values(graph_version="dynamic-investigation-graph-v9")
        )
    claim = await store.claim_next(
        worker_id="dynamic-v9-second-round-review-worker",
        occurred_at=BASE_TIME + timedelta(seconds=2),
        lease_duration=timedelta(seconds=30),
        supported_workflow_versions=(WorkflowVersion.V3,),
    )
    assert claim is not None
    return store, claim


def review_factory(db, source, client, saver, clock):
    return DynamicSecondRoundReviewGraphFactory(
        session_factory=db,
        data_source=source,
        model=sdk_model(client),
        checkpointer=saver,
        clock=clock,
    )


@pytest.mark.parametrize(
    ("route_case", "expects_follow_up"),
    [("follow_up", True), ("candidate", False), ("human", False)],
)
async def test_second_round_review_is_scoped_to_follow_up_materials(
    database_session_factory,
    checkpoint_database_url,
    route_case,
    expects_follow_up,
) -> None:
    await initialize_checkpoint_schema(checkpoint_database_url)
    store, claim = await current_v9_claim(database_session_factory)
    source = RecordingFrozenSource(claim.request)
    replies = SecondRoundReviewReplies(
        database_session_factory,
        (EvidenceKind.LOG, EvidenceKind.METRIC),
        route_case=route_case,
    )

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

    hypotheses = await OpenHypothesisQueries(database_session_factory).list_for_run(claim.run.id)
    reviews = await HypothesisReviewQueries(database_session_factory).list_for_run(claim.run.id)
    budget = await CallBudgetQueries(database_session_factory).get_snapshot(claim.run.id)
    synthesis_calls = [item for item in budget.calls if item.operation is CallOperation.SYNTHESIZE]
    review_calls = [item for item in budget.calls if item.operation is CallOperation.REVIEW]
    assert checkpoint is not None
    assert checkpoint.metadata["graph_version"] == "dynamic-investigation-graph-v9"
    assert len(hypotheses) == (2 if expects_follow_up else 1)
    assert len(synthesis_calls) == (2 if expects_follow_up else 1)
    assert len(reviews) == (2 if expects_follow_up else 1)
    assert len(review_calls) == (2 if expects_follow_up else 1)
    assert [item.invocation_index for item in synthesis_calls] == (
        [0, 1] if expects_follow_up else [0]
    )
    assert len(replies.synthesis_inputs) == (2 if expects_follow_up else 1)
    assert len(replies.review_inputs) == (2 if expects_follow_up else 1)
    assert result.follow_up_hypothesis == (hypotheses[1] if expects_follow_up else None)
    assert result.follow_up_hypothesis_review == (reviews[1] if expects_follow_up else None)
    assert [item.invocation_index for item in review_calls] == (
        [0, 1] if expects_follow_up else [0]
    )

    if expects_follow_up:
        with pytest.raises(TaskFactConflict) as unsupported:
            await SecondRoundReviewRoutingService(
                database_session_factory,
                run_id=claim.run.id,
                lease_token=claim.work_item.lease_token,
                clock=TaskClock(),
            ).route(result)
        assert unsupported.value.code == "review_routing_not_supported"
        snapshot = await CollaborationQueries(database_session_factory).get_snapshot(claim.run.id)
        first_task_ids = {task.id for task in snapshot.rounds[0].tasks}
        second_task_ids = {task.id for task in snapshot.rounds[1].tasks}
        first_manifest = await CallBudgetQueries(database_session_factory).get_context_manifest(
            claim.run.id, synthesis_calls[0].id
        )
        second_manifest = await CallBudgetQueries(database_session_factory).get_context_manifest(
            claim.run.id, synthesis_calls[1].id
        )
        first_review_manifest = await CallBudgetQueries(
            database_session_factory
        ).get_context_manifest(claim.run.id, review_calls[0].id)
        second_review_manifest = await CallBudgetQueries(
            database_session_factory
        ).get_context_manifest(claim.run.id, review_calls[1].id)
        assert (
            first_manifest is not None
            and second_manifest is not None
            and first_review_manifest is not None
            and second_review_manifest is not None
        )
        assert {
            item.material_id
            for item in first_manifest.materials
            if item.kind == "task_working_summary"
        } == first_task_ids
        assert {
            item.material_id
            for item in second_manifest.materials
            if item.kind == "task_working_summary"
        } == second_task_ids
        assert first_task_ids.isdisjoint(second_task_ids)
        assert second_manifest.role_version == "dynamic-follow-up-synthesis-v1"
        assert second_manifest.prompt_version == "follow-up-open-hypothesis-synthesis-v1"
        assert first_review_manifest.materials[:-1] == first_manifest.materials
        assert second_review_manifest.materials[:-1] == second_manifest.materials
        assert second_review_manifest.materials[-1].material_id == hypotheses[1].id
        assert first_review_manifest.role_version == "independent-review-v1"
        assert first_review_manifest.prompt_version == "open-hypothesis-review-v1"
        assert second_review_manifest.role_version == "independent-follow-up-review-v1"
        assert second_review_manifest.prompt_version == "follow-up-open-hypothesis-review-v1"
        assert second_review_manifest.tool_names == ()
        assert replies.synthesis_prompts[-1] == FOLLOW_UP_OPEN_HYPOTHESIS_PROMPT
        assert replies.review_prompts == [
            HYPOTHESIS_REVIEW_PROMPT,
            FOLLOW_UP_HYPOTHESIS_REVIEW_PROMPT,
        ]
        assert replies.review_inputs[-1]["open_hypothesis"] == hypotheses[1].model_dump(mode="json")
        assert (
            replies.review_inputs[-1]["working_summaries"]
            == replies.synthesis_inputs[-1]["working_summaries"]
        )
        assert budget.reserved_model_calls == 11
    else:
        assert budget.reserved_model_calls == 7
    run = await store.get_run(claim.run.id)
    assert run.status is InvestigationRunStatus.RUNNING and run.result is None


async def test_second_round_review_reuses_successful_call_before_review_commit(
    database_session_factory,
    checkpoint_database_url,
    monkeypatch,
) -> None:
    await initialize_checkpoint_schema(checkpoint_database_url)
    store, claim = await current_v9_claim(database_session_factory)
    clock = TaskClock()
    source = RecordingFrozenSource(claim.request)
    replies = SecondRoundReviewReplies(
        database_session_factory,
        (EvidenceKind.LOG,),
        route_case="follow_up",
    )
    original = HypothesisReviewStore.save
    save_count = 0

    async def crash_before_second_save(self, *args, **kwargs):
        nonlocal save_count
        save_count += 1
        if save_count == 2:
            raise RuntimeError("模拟第二轮审查调用成功后、事实提交前中断")
        return await original(self, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(HypothesisReviewStore, "save", crash_before_second_save)
        async with (
            httpx2.AsyncClient(transport=httpx2.MockTransport(replies)) as client,
            open_checkpoint_saver(checkpoint_database_url) as saver,
        ):
            graph = review_factory(database_session_factory, source, client, saver, clock)(
                processor_context(claim)
            )
            with pytest.raises(RuntimeError, match="事实提交前中断"):
                await graph.investigate(claim.request, thread_id=claim.run.graph_thread_id)

    before = await CallBudgetQueries(database_session_factory).get_snapshot(claim.run.id)
    assert Counter(item.operation for item in before.calls)[CallOperation.REVIEW] == 2
    assert (
        len(await HypothesisReviewQueries(database_session_factory).list_for_run(claim.run.id)) == 1
    )
    await store.fail(
        claim.work_item.id,
        lease_token=claim.work_item.lease_token,
        occurred_at=clock(),
        error=WorkItemError(error_code="crash", message="第二轮审查事实提交前中断"),
        retry_delay=timedelta(0),
    )
    clock.current += timedelta(seconds=1)
    replacement = await store.claim_next(
        worker_id="replacement-before-follow-up-review",
        occurred_at=clock(),
        lease_duration=timedelta(seconds=30),
        supported_workflow_versions=(WorkflowVersion.V3,),
    )
    assert replacement is not None
    recovery_source = RecordingFrozenSource(replacement.request)
    async with (
        httpx2.AsyncClient(
            transport=httpx2.MockTransport(
                lambda _: pytest.fail("成功的第二轮审查调用恢复不应再次请求模型")
            )
        ) as client,
        open_checkpoint_saver(checkpoint_database_url) as saver,
    ):
        graph = review_factory(database_session_factory, recovery_source, client, saver, clock)(
            processor_context(replacement)
        )
        result = await graph.resume(thread_id=replacement.run.graph_thread_id)

    reviews = await HypothesisReviewQueries(database_session_factory).list_for_run(claim.run.id)
    assert len(reviews) == 2
    assert result.follow_up_hypothesis_review == reviews[1]
    assert await CallBudgetQueries(database_session_factory).get_snapshot(claim.run.id) == before
    assert all(not recovery_source.calls[role] for role in EvidenceKind)


async def test_second_round_review_reuses_review_committed_before_checkpoint(
    database_session_factory,
    checkpoint_database_url,
    monkeypatch,
) -> None:
    await initialize_checkpoint_schema(checkpoint_database_url)
    store, claim = await current_v9_claim(database_session_factory)
    clock = TaskClock()
    source = RecordingFrozenSource(claim.request)
    replies = SecondRoundReviewReplies(
        database_session_factory,
        (EvidenceKind.METRIC,),
        route_case="follow_up",
    )
    original = HypothesisReviewStore.save
    save_count = 0

    async def save_second_then_crash(self, *args, **kwargs):
        nonlocal save_count
        save_count += 1
        result = await original(self, *args, **kwargs)
        if save_count == 2:
            raise RuntimeError("模拟第二轮审查事实提交后 checkpoint 前中断")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(HypothesisReviewStore, "save", save_second_then_crash)
        async with (
            httpx2.AsyncClient(transport=httpx2.MockTransport(replies)) as client,
            open_checkpoint_saver(checkpoint_database_url) as saver,
        ):
            graph = review_factory(database_session_factory, source, client, saver, clock)(
                processor_context(claim)
            )
            with pytest.raises(RuntimeError, match="checkpoint 前中断"):
                await graph.investigate(claim.request, thread_id=claim.run.graph_thread_id)

    before = await CallBudgetQueries(database_session_factory).get_snapshot(claim.run.id)
    reviews = await HypothesisReviewQueries(database_session_factory).list_for_run(claim.run.id)
    assert len(reviews) == 2
    await store.fail(
        claim.work_item.id,
        lease_token=claim.work_item.lease_token,
        occurred_at=clock(),
        error=WorkItemError(error_code="crash", message="第二轮审查 checkpoint 窗口中断"),
        retry_delay=timedelta(0),
    )
    clock.current += timedelta(seconds=1)
    replacement = await store.claim_next(
        worker_id="replacement-after-follow-up-review",
        occurred_at=clock(),
        lease_duration=timedelta(seconds=30),
        supported_workflow_versions=(WorkflowVersion.V3,),
    )
    assert replacement is not None
    recovery_source = RecordingFrozenSource(replacement.request)
    async with (
        httpx2.AsyncClient(
            transport=httpx2.MockTransport(
                lambda _: pytest.fail("已提交第二轮审查恢复不应再次请求模型")
            )
        ) as client,
        open_checkpoint_saver(checkpoint_database_url) as saver,
    ):
        graph = review_factory(database_session_factory, recovery_source, client, saver, clock)(
            processor_context(replacement)
        )
        result = await graph.resume(thread_id=replacement.run.graph_thread_id)

    assert result.follow_up_hypothesis_review == reviews[1]
    assert await CallBudgetQueries(database_session_factory).get_snapshot(claim.run.id) == before
    assert all(not recovery_source.calls[role] for role in EvidenceKind)


async def test_second_round_review_rejects_multiple_successful_calls_in_same_slot(
    database_session_factory,
    checkpoint_database_url,
) -> None:
    await initialize_checkpoint_schema(checkpoint_database_url)
    store, claim = await current_v9_claim(database_session_factory)
    clock = TaskClock()
    replies = SecondRoundReviewReplies(
        database_session_factory,
        (EvidenceKind.CHANGE,),
        route_case="follow_up",
    )
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
        await graph.investigate(claim.request, thread_id=claim.run.graph_thread_id)

    queries = CallBudgetQueries(database_session_factory)
    round_two = (
        await HypothesisReviewQueries(database_session_factory).list_for_run(claim.run.id)
    )[1]
    snapshot = await queries.get_snapshot(claim.run.id)
    second_call = next(
        item
        for item in snapshot.calls
        if item.operation is CallOperation.REVIEW and item.invocation_index == 1
    )
    model_input = await queries.get_model_input(claim.run.id, second_call.id)
    manifest = await queries.get_context_manifest(claim.run.id, second_call.id)
    interaction = await queries.get_interaction(claim.run.id, second_call.id)
    assert model_input is not None and manifest is not None
    result = ReviewModelResult.model_validate(interaction.result)

    await store.fail(
        claim.work_item.id,
        lease_token=claim.work_item.lease_token,
        occurred_at=clock(),
        error=WorkItemError(error_code="crash", message="构造跨父尝试同槽位来源冲突"),
        retry_delay=timedelta(0),
    )
    clock.current += timedelta(seconds=1)
    replacement = await store.claim_next(
        worker_id="duplicate-review-source",
        occurred_at=clock(),
        lease_duration=timedelta(seconds=30),
        supported_workflow_versions=(WorkflowVersion.V3,),
    )
    assert replacement is not None
    calls = CallBudgetStore(database_session_factory, clock=clock)
    with pytest.raises(TaskFactConflict) as invalid_slot:
        await calls.reserve(
            claim.run.id,
            lease_token=replacement.work_item.lease_token,
            operation=CallOperation.REVIEW,
            model_input=model_input,
            prompt_version=manifest.prompt_version,
            context_manifest=manifest,
            invocation_index=3,
        )
    assert invalid_slot.value.code == "hypothesis_review_invocation_invalid"
    grant = await calls.reserve(
        claim.run.id,
        lease_token=replacement.work_item.lease_token,
        operation=CallOperation.REVIEW,
        model_input=model_input,
        prompt_version=manifest.prompt_version,
        context_manifest=manifest,
        invocation_index=1,
    )
    assert grant.created
    await calls.settle(
        claim.run.id,
        grant.record.id,
        lease_token=replacement.work_item.lease_token,
        status="succeeded",
        call_result=result,
    )

    with pytest.raises(TaskFactConflict) as caught:
        await HypothesisReviewQueries(database_session_factory).list_for_run(claim.run.id)
    assert caught.value.code == "hypothesis_review_call_conflict"
    with pytest.raises(TaskFactConflict) as caught:
        await HypothesisReviewStore(database_session_factory, clock=clock).get_existing(
            claim.run.id,
            round_two.open_hypothesis_id,
            lease_token=replacement.work_item.lease_token,
        )
    assert caught.value.code == "hypothesis_review_call_conflict"
