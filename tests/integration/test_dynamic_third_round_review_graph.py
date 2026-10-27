"""真实 PostgreSQL 与 checkpoint 验证第三轮独立审查、材料隔离与恢复。"""

import asyncio
import json
from datetime import timedelta

import httpx2
import pytest
from sqlalchemy import text, update
from sqlalchemy.exc import DBAPIError

from alembic import command
from opsswarm.checkpoints import initialize_checkpoint_schema, open_checkpoint_saver
from opsswarm.investigation.budget.contracts import CallBudgetPolicy, CallOperation
from opsswarm.investigation.budget.queries import CallBudgetQueries
from opsswarm.investigation.context.prompts import THIRD_ROUND_HYPOTHESIS_REVIEW_PROMPT
from opsswarm.investigation.models import EvidenceKind, InvestigationRunStatus
from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.investigation.review.persistence import HypothesisReviewStore
from opsswarm.investigation.review.queries import HypothesisReviewQueries
from opsswarm.investigation.synthesis.queries import OpenHypothesisQueries
from opsswarm.investigation.tasks.errors import TaskFactConflict
from opsswarm.investigation.workflow_versions import WorkflowVersion
from opsswarm.worker.dynamic_graph import DynamicThirdRoundReviewGraphFactory
from tests.integration.test_dynamic_first_round_graph import (
    RecordingFrozenSource,
    processor_context,
)
from tests.integration.test_dynamic_third_round_synthesis_graph import (
    ThirdRoundSynthesisReplies,
    _fail_and_reclaim,
    migration_config,
)
from tests.loop_helpers import loop_policy
from tests.model_budget_helpers import sdk_model, sdk_response
from tests.task_facts_helpers import BASE_TIME, TaskClock, started_run

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


class ThirdRoundReviewReplies(ThirdRoundSynthesisReplies):
    """记录三轮审查提示，并允许第三轮返回越界支持证据。"""

    def __init__(
        self,
        db,
        *,
        second_route_case: str,
        invalid_third_evidence: bool = False,
    ) -> None:
        super().__init__(db, second_route_case=second_route_case)
        self.review_prompts: list[str] = []
        self.invalid_third_review_evidence = invalid_third_evidence

    async def __call__(self, request):
        payload = json.loads(request.content)
        data = json.loads(payload["messages"][1]["content"])
        prompt = payload["messages"][0]["content"]
        if "open_hypothesis" in data:
            self.review_prompts.append(prompt)
        if "open_hypothesis" in data and prompt == THIRD_ROUND_HYPOTHESIS_REVIEW_PROMPT:
            self.review_inputs.append(data)
            hypothesis = data["open_hypothesis"]["hypothesis"]
            accepted = (
                ["previous-round-only"]
                if self.invalid_third_review_evidence
                else hypothesis["supporting_evidence_ids"][:1]
            )
            return sdk_response(
                payload,
                {
                    "support_is_sufficient": True,
                    "rationale": "第三轮同源材料足以进入最大轮次后的确定性评估。",
                    "accepted_supporting_evidence_ids": accepted,
                    "counter_evidence_ids": hypothesis["counter_evidence_ids"],
                    "evidence_gaps": [],
                    "competing_explanations": [],
                    "suggested_direction": "evaluate_for_routing",
                    "follow_up_suggestions": [],
                },
            )
        return await super().__call__(request)


async def current_v14_claim(database_session_factory):
    store, started = await started_run(
        database_session_factory,
        workflow_version=WorkflowVersion.V3,
        call_budget=CallBudgetPolicy(max_model_calls=13),
        model_budget=loop_policy(),
    )
    async with database_session_factory() as session, session.begin():
        await session.execute(
            update(InvestigationRunRow)
            .where(InvestigationRunRow.id == started.run.id)
            .values(graph_version="dynamic-investigation-graph-v14")
        )
    claim = await store.claim_next(
        worker_id="dynamic-v14-third-round-review-worker",
        occurred_at=BASE_TIME + timedelta(seconds=2),
        lease_duration=timedelta(seconds=30),
        supported_workflow_versions=(WorkflowVersion.V3,),
    )
    assert claim is not None
    assert claim.run.graph_version == "dynamic-investigation-graph-v14"
    return store, claim


def review_factory(db, source, client, saver, clock):
    return DynamicThirdRoundReviewGraphFactory(
        session_factory=db,
        data_source=source,
        model=sdk_model(client),
        checkpointer=saver,
        clock=clock,
    )


@pytest.mark.parametrize(
    ("second_route_case", "expected_facts", "expected_models", "expected_tools"),
    [
        ("follow_up", 3, 13, 6),
        ("human", 2, 9, 4),
        ("candidate", 2, 9, 4),
    ],
)
async def test_third_round_review_only_uses_third_round_hypothesis_materials(
    database_session_factory,
    checkpoint_database_url,
    second_route_case,
    expected_facts,
    expected_models,
    expected_tools,
) -> None:
    await initialize_checkpoint_schema(checkpoint_database_url)
    store, claim = await current_v14_claim(database_session_factory)
    replies = ThirdRoundReviewReplies(
        database_session_factory,
        second_route_case=second_route_case,
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
            TaskClock(),
        )(processor_context(claim))
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
    expected_slots = [0, 1, 2] if second_route_case == "follow_up" else [0, 1]
    assert len(hypotheses) == expected_facts
    assert len(reviews) == expected_facts
    assert [item.invocation_index for item in synthesis_calls] == expected_slots
    assert [item.invocation_index for item in review_calls] == expected_slots
    assert budget.reserved_model_calls == expected_models
    assert budget.reserved_tool_calls == expected_tools
    assert checkpoint is not None
    assert checkpoint.metadata["graph_version"] == "dynamic-investigation-graph-v14"

    if second_route_case == "follow_up":
        assert result.third_round_hypothesis == hypotheses[2]
        assert result.third_round_hypothesis_review == reviews[2]
        synthesis_manifest = await CallBudgetQueries(database_session_factory).get_context_manifest(
            claim.run.id, synthesis_calls[2].id
        )
        review_manifest = await CallBudgetQueries(database_session_factory).get_context_manifest(
            claim.run.id, review_calls[2].id
        )
        assert synthesis_manifest is not None and review_manifest is not None
        assert review_manifest.materials[:-1] == synthesis_manifest.materials
        assert review_manifest.materials[-1].kind == "open_hypothesis"
        assert review_manifest.materials[-1].material_id == hypotheses[2].id
        assert review_manifest.role_version == "independent-third-round-review-v1"
        assert review_manifest.prompt_version == "third-round-open-hypothesis-review-v1"
        assert review_manifest.tool_names == ()
        assert replies.review_prompts[-1] == THIRD_ROUND_HYPOTHESIS_REVIEW_PROMPT
        assert (
            replies.review_inputs[-1]["working_summaries"]
            == replies.synthesis_inputs[-1]["working_summaries"]
        )
    else:
        assert result.third_round_hypothesis is None
        assert result.third_round_hypothesis_review is None
    run = await store.get_run(claim.run.id)
    assert run.status is InvestigationRunStatus.RUNNING and run.result is None


async def test_third_round_review_reuses_successful_call_before_fact_commit(
    database_session_factory,
    checkpoint_database_url,
    monkeypatch,
) -> None:
    await initialize_checkpoint_schema(checkpoint_database_url)
    store, claim = await current_v14_claim(database_session_factory)
    clock = TaskClock()
    replies = ThirdRoundReviewReplies(database_session_factory, second_route_case="follow_up")
    original = HypothesisReviewStore.save
    save_count = 0

    async def crash_before_third_save(self, *args, **kwargs):
        nonlocal save_count
        save_count += 1
        if save_count == 3:
            raise RuntimeError("模拟第三轮审查调用成功后、事实提交前中断")
        return await original(self, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(HypothesisReviewStore, "save", crash_before_third_save)
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
            with pytest.raises(RuntimeError, match="事实提交前中断"):
                await graph.investigate(claim.request, thread_id=claim.run.graph_thread_id)

    before = await CallBudgetQueries(database_session_factory).get_snapshot(claim.run.id)
    assert [
        item.invocation_index for item in before.calls if item.operation is CallOperation.REVIEW
    ] == [0, 1, 2]
    assert (
        len(await HypothesisReviewQueries(database_session_factory).list_for_run(claim.run.id)) == 2
    )
    await _fail_and_reclaim(store, claim, clock)
    replacement = await store.claim_next(
        worker_id="replacement-before-third-review",
        occurred_at=clock(),
        lease_duration=timedelta(seconds=30),
        supported_workflow_versions=(WorkflowVersion.V3,),
    )
    assert replacement is not None
    recovery_source = RecordingFrozenSource(replacement.request)
    async with (
        httpx2.AsyncClient(
            transport=httpx2.MockTransport(
                lambda _: pytest.fail("成功的第三轮审查调用恢复不应再次请求模型")
            )
        ) as client,
        open_checkpoint_saver(checkpoint_database_url) as saver,
    ):
        graph = review_factory(database_session_factory, recovery_source, client, saver, clock)(
            processor_context(replacement)
        )
        result = await graph.resume(thread_id=replacement.run.graph_thread_id)

    reviews = await HypothesisReviewQueries(database_session_factory).list_for_run(claim.run.id)
    assert len(reviews) == 3 and result.third_round_hypothesis_review == reviews[2]
    assert await CallBudgetQueries(database_session_factory).get_snapshot(claim.run.id) == before
    assert all(not recovery_source.calls[role] for role in EvidenceKind)


async def test_third_round_review_reuses_fact_committed_before_checkpoint(
    database_session_factory,
    checkpoint_database_url,
    monkeypatch,
) -> None:
    await initialize_checkpoint_schema(checkpoint_database_url)
    store, claim = await current_v14_claim(database_session_factory)
    clock = TaskClock()
    replies = ThirdRoundReviewReplies(database_session_factory, second_route_case="follow_up")
    original = HypothesisReviewStore.save
    save_count = 0

    async def save_third_then_crash(self, *args, **kwargs):
        nonlocal save_count
        save_count += 1
        result = await original(self, *args, **kwargs)
        if save_count == 3:
            raise RuntimeError("模拟第三轮审查事实提交后 checkpoint 前中断")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(HypothesisReviewStore, "save", save_third_then_crash)
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
    assert len(reviews) == 3
    await _fail_and_reclaim(store, claim, clock)
    replacement = await store.claim_next(
        worker_id="replacement-after-third-review",
        occurred_at=clock(),
        lease_duration=timedelta(seconds=30),
        supported_workflow_versions=(WorkflowVersion.V3,),
    )
    assert replacement is not None
    recovery_source = RecordingFrozenSource(replacement.request)
    async with (
        httpx2.AsyncClient(
            transport=httpx2.MockTransport(
                lambda _: pytest.fail("已提交第三轮审查恢复不应再次请求模型")
            )
        ) as client,
        open_checkpoint_saver(checkpoint_database_url) as saver,
    ):
        graph = review_factory(database_session_factory, recovery_source, client, saver, clock)(
            processor_context(replacement)
        )
        result = await graph.resume(thread_id=replacement.run.graph_thread_id)

    assert result.third_round_hypothesis_review == reviews[2]
    assert await CallBudgetQueries(database_session_factory).get_snapshot(claim.run.id) == before
    assert all(not recovery_source.calls[role] for role in EvidenceKind)


async def test_third_round_review_rejects_evidence_outside_third_round_hypothesis(
    database_session_factory,
    checkpoint_database_url,
) -> None:
    await initialize_checkpoint_schema(checkpoint_database_url)
    _, claim = await current_v14_claim(database_session_factory)
    replies = ThirdRoundReviewReplies(
        database_session_factory,
        second_route_case="follow_up",
        invalid_third_evidence=True,
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
            TaskClock(),
        )(processor_context(claim))
        with pytest.raises(TaskFactConflict, match="开放假设未引用"):
            await graph.investigate(claim.request, thread_id=claim.run.graph_thread_id)

    reviews = await HypothesisReviewQueries(database_session_factory).list_for_run(claim.run.id)
    assert len(reviews) == 2


async def test_third_round_review_call_blocks_downgrade_without_v14_run(
    database_session_factory,
    checkpoint_database_url,
) -> None:
    await initialize_checkpoint_schema(checkpoint_database_url)
    _, claim = await current_v14_claim(database_session_factory)
    replies = ThirdRoundReviewReplies(database_session_factory, second_route_case="follow_up")
    async with (
        httpx2.AsyncClient(transport=httpx2.MockTransport(replies)) as client,
        open_checkpoint_saver(checkpoint_database_url) as saver,
    ):
        graph = review_factory(
            database_session_factory,
            RecordingFrozenSource(claim.request),
            client,
            saver,
            TaskClock(),
        )(processor_context(claim))
        await graph.investigate(claim.request, thread_id=claim.run.graph_thread_id)

    async with database_session_factory() as session, session.begin():
        await session.execute(
            update(InvestigationRunRow)
            .where(InvestigationRunRow.id == claim.run.id)
            .values(graph_version="dynamic-investigation-graph-v13")
        )

    with pytest.raises(DBAPIError, match="不能删除已保存的第三轮独立审查调用事实"):
        await asyncio.to_thread(command.downgrade, migration_config(), "0055_third_round_synthesis")
    async with database_session_factory() as session:
        assert await session.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0084_candidate_settlement"
        )


async def test_third_round_review_fact_blocks_downgrade_without_v14_or_slot_two(
    database_session_factory,
    checkpoint_database_url,
) -> None:
    await initialize_checkpoint_schema(checkpoint_database_url)
    store, claim = await current_v14_claim(database_session_factory)
    clock = TaskClock()
    replies = ThirdRoundReviewReplies(database_session_factory, second_route_case="follow_up")
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

    review = (await HypothesisReviewQueries(database_session_factory).list_for_run(claim.run.id))[2]
    await _fail_and_reclaim(store, claim, clock)
    replacement = await store.claim_next(
        worker_id="replacement-for-review-migration-guard",
        occurred_at=clock(),
        lease_duration=timedelta(seconds=30),
        supported_workflow_versions=(WorkflowVersion.V3,),
    )
    assert replacement is not None
    async with database_session_factory() as session, session.begin():
        await session.execute(
            text(
                "ALTER TABLE investigation_hypothesis_reviews "
                "DISABLE TRIGGER trg_hypothesis_reviews_immutable"
            )
        )
        await session.execute(
            text(
                "UPDATE investigation_call_reservations "
                "SET investigation_attempt_id = :attempt_id, invocation_index = 1, "
                "idempotency_key = :idempotency_key WHERE id = :call_id"
            ),
            {
                "attempt_id": replacement.attempt.id,
                "idempotency_key": f"{replacement.attempt.id}:root:review:1",
                "call_id": review.source_call_id,
            },
        )
        await session.execute(
            text(
                "UPDATE investigation_hypothesis_reviews "
                "SET investigation_attempt_id = :attempt_id WHERE id = :review_id"
            ),
            {"attempt_id": replacement.attempt.id, "review_id": review.id},
        )
        await session.execute(
            text(
                "ALTER TABLE investigation_hypothesis_reviews "
                "ENABLE TRIGGER trg_hypothesis_reviews_immutable"
            )
        )
        await session.execute(
            update(InvestigationRunRow)
            .where(InvestigationRunRow.id == claim.run.id)
            .values(graph_version="dynamic-investigation-graph-v13")
        )

    with pytest.raises(DBAPIError, match="不能删除已保存的第三轮独立审查事实"):
        await asyncio.to_thread(command.downgrade, migration_config(), "0055_third_round_synthesis")
    async with database_session_factory() as session:
        assert await session.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0084_candidate_settlement"
        )
