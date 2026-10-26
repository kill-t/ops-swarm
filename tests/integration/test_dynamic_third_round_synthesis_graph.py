"""真实 PostgreSQL 与 checkpoint 验证第三轮综合、材料隔离与恢复。"""

import asyncio
import json
from datetime import timedelta
from pathlib import Path

import httpx2
import pytest
from alembic.config import Config
from sqlalchemy import text, update
from sqlalchemy.exc import DBAPIError

from alembic import command
from opsswarm.checkpoints import initialize_checkpoint_schema, open_checkpoint_saver
from opsswarm.investigation.budget.contracts import CallOperation
from opsswarm.investigation.budget.queries import CallBudgetQueries
from opsswarm.investigation.context.prompts import THIRD_ROUND_OPEN_HYPOTHESIS_PROMPT
from opsswarm.investigation.models import EvidenceKind, InvestigationRunStatus, WorkItemError
from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.investigation.synthesis.persistence import OpenHypothesisStore
from opsswarm.investigation.synthesis.queries import OpenHypothesisQueries
from opsswarm.investigation.tasks.errors import TaskFactConflict
from opsswarm.investigation.tasks.queries import CollaborationQueries
from opsswarm.investigation.workflow_versions import WorkflowVersion
from opsswarm.worker.dynamic_graph import DynamicThirdRoundSynthesisGraphFactory
from tests.integration.test_dynamic_first_round_graph import (
    RecordingFrozenSource,
    processor_context,
)
from tests.integration.test_dynamic_third_round_planning_graph import SecondRoundRoutingReplies
from tests.loop_helpers import loop_policy
from tests.model_budget_helpers import sdk_model, sdk_response
from tests.task_facts_helpers import BASE_TIME, TaskClock, started_run

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def migration_config() -> Config:
    return Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))


class ThirdRoundSynthesisReplies(SecondRoundRoutingReplies):
    """记录三轮综合提示，并允许第三轮返回越界证据。"""

    def __init__(self, db, *, second_route_case: str, invalid_third_evidence: bool = False):
        super().__init__(db, (EvidenceKind.LOG,), second_route_case=second_route_case)
        self.synthesis_prompts: list[str] = []
        self.invalid_third_evidence = invalid_third_evidence

    async def __call__(self, request):
        payload = json.loads(request.content)
        data = json.loads(payload["messages"][1]["content"])
        if "working_summaries" in data and "open_hypothesis" not in data:
            self.synthesis_prompts.append(payload["messages"][0]["content"])
            if self.invalid_third_evidence and len(self.synthesis_inputs) == 2:
                return sdk_response(
                    payload,
                    {
                        "statement": "错误引用前轮材料",
                        "confidence": 0.4,
                        "supporting_evidence_ids": ["previous-round-only"],
                        "counter_evidence_ids": [],
                        "unresolved_questions": ["第三轮材料能否支持该判断"],
                    },
                )
        return await super().__call__(request)


async def current_v13_claim(database_session_factory):
    store, started = await started_run(
        database_session_factory,
        workflow_version=WorkflowVersion.V3,
        model_budget=loop_policy(),
    )
    async with database_session_factory() as session, session.begin():
        await session.execute(
            update(InvestigationRunRow)
            .where(InvestigationRunRow.id == started.run.id)
            .values(graph_version="dynamic-investigation-graph-v13")
        )
    claim = await store.claim_next(
        worker_id="dynamic-v13-third-round-synthesis-worker",
        occurred_at=BASE_TIME + timedelta(seconds=2),
        lease_duration=timedelta(seconds=30),
        supported_workflow_versions=(WorkflowVersion.V3,),
    )
    assert claim is not None
    assert claim.run.graph_version == "dynamic-investigation-graph-v13"
    return store, claim


def synthesis_factory(db, source, client, saver, clock):
    return DynamicThirdRoundSynthesisGraphFactory(
        session_factory=db,
        data_source=source,
        model=sdk_model(client),
        checkpointer=saver,
        clock=clock,
    )


@pytest.mark.parametrize(
    ("second_route_case", "expected_hypotheses", "expected_models", "expected_tools"),
    [
        ("follow_up", 3, 12, 6),
        ("human", 2, 9, 4),
        ("candidate", 2, 9, 4),
    ],
)
async def test_third_round_synthesis_only_uses_persisted_third_round_materials(
    database_session_factory,
    checkpoint_database_url,
    second_route_case,
    expected_hypotheses,
    expected_models,
    expected_tools,
) -> None:
    await initialize_checkpoint_schema(checkpoint_database_url)
    store, claim = await current_v13_claim(database_session_factory)
    source = RecordingFrozenSource(claim.request)
    replies = ThirdRoundSynthesisReplies(
        database_session_factory,
        second_route_case=second_route_case,
    )

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

    hypotheses = await OpenHypothesisQueries(database_session_factory).list_for_run(claim.run.id)
    budget = await CallBudgetQueries(database_session_factory).get_snapshot(claim.run.id)
    synthesis_calls = [item for item in budget.calls if item.operation is CallOperation.SYNTHESIZE]
    assert len(hypotheses) == expected_hypotheses
    assert [item.invocation_index for item in synthesis_calls] == (
        [0, 1, 2] if second_route_case == "follow_up" else [0, 1]
    )
    assert budget.reserved_model_calls == expected_models
    assert budget.reserved_tool_calls == expected_tools
    assert checkpoint is not None
    assert checkpoint.metadata["graph_version"] == "dynamic-investigation-graph-v13"

    if second_route_case == "follow_up":
        assert result.third_round_hypothesis == hypotheses[2]
        assert result.third_round_hypothesis.status == "pending_review"
        snapshot = await CollaborationQueries(database_session_factory).get_snapshot(claim.run.id)
        round_task_ids = [{task.id for task in round_row.tasks} for round_row in snapshot.rounds]
        manifests = [
            await CallBudgetQueries(database_session_factory).get_context_manifest(
                claim.run.id, call.id
            )
            for call in synthesis_calls
        ]
        assert all(item is not None for item in manifests)
        manifest_task_ids = [
            {
                material.material_id
                for material in manifest.materials
                if material.kind == "task_working_summary"
            }
            for manifest in manifests
            if manifest is not None
        ]
        assert manifest_task_ids == round_task_ids
        assert all(
            left.isdisjoint(right)
            for index, left in enumerate(manifest_task_ids)
            for right in manifest_task_ids[index + 1 :]
        )
        third_manifest = manifests[2]
        assert third_manifest is not None
        assert third_manifest.role_version == "dynamic-third-round-synthesis-v1"
        assert third_manifest.prompt_version == "third-round-open-hypothesis-synthesis-v1"
        assert replies.synthesis_prompts[-1] == THIRD_ROUND_OPEN_HYPOTHESIS_PROMPT
    else:
        assert result.third_round_hypothesis is None
    run = await store.get_run(claim.run.id)
    assert run.status is InvestigationRunStatus.RUNNING and run.result is None


async def test_third_round_synthesis_reuses_successful_call_before_hypothesis_commit(
    database_session_factory,
    checkpoint_database_url,
    monkeypatch,
) -> None:
    await initialize_checkpoint_schema(checkpoint_database_url)
    store, claim = await current_v13_claim(database_session_factory)
    clock = TaskClock()
    replies = ThirdRoundSynthesisReplies(database_session_factory, second_route_case="follow_up")
    original = OpenHypothesisStore.save
    save_count = 0

    async def crash_before_third_save(self, *args, **kwargs):
        nonlocal save_count
        save_count += 1
        if save_count == 3:
            raise RuntimeError("模拟第三轮模型成功后、假设提交前中断")
        return await original(self, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(OpenHypothesisStore, "save", crash_before_third_save)
        async with (
            httpx2.AsyncClient(transport=httpx2.MockTransport(replies)) as client,
            open_checkpoint_saver(checkpoint_database_url) as saver,
        ):
            graph = synthesis_factory(
                database_session_factory,
                RecordingFrozenSource(claim.request),
                client,
                saver,
                clock,
            )(processor_context(claim))
            with pytest.raises(RuntimeError, match="假设提交前中断"):
                await graph.investigate(claim.request, thread_id=claim.run.graph_thread_id)

    before = await CallBudgetQueries(database_session_factory).get_snapshot(claim.run.id)
    assert [
        item.invocation_index for item in before.calls if item.operation is CallOperation.SYNTHESIZE
    ] == [0, 1, 2]
    assert (
        len(await OpenHypothesisQueries(database_session_factory).list_for_run(claim.run.id)) == 2
    )
    await _fail_and_reclaim(store, claim, clock)
    replacement = await store.claim_next(
        worker_id="replacement-before-third-hypothesis",
        occurred_at=clock(),
        lease_duration=timedelta(seconds=30),
        supported_workflow_versions=(WorkflowVersion.V3,),
    )
    assert replacement is not None
    recovery_source = RecordingFrozenSource(replacement.request)
    async with (
        httpx2.AsyncClient(
            transport=httpx2.MockTransport(
                lambda _: pytest.fail("成功的第三轮综合调用恢复不应再次请求模型")
            )
        ) as client,
        open_checkpoint_saver(checkpoint_database_url) as saver,
    ):
        graph = synthesis_factory(database_session_factory, recovery_source, client, saver, clock)(
            processor_context(replacement)
        )
        result = await graph.resume(thread_id=replacement.run.graph_thread_id)

    hypotheses = await OpenHypothesisQueries(database_session_factory).list_for_run(claim.run.id)
    assert len(hypotheses) == 3 and result.third_round_hypothesis == hypotheses[2]
    assert await CallBudgetQueries(database_session_factory).get_snapshot(claim.run.id) == before
    assert all(not recovery_source.calls[role] for role in EvidenceKind)


async def test_third_round_synthesis_reuses_hypothesis_committed_before_checkpoint(
    database_session_factory,
    checkpoint_database_url,
    monkeypatch,
) -> None:
    await initialize_checkpoint_schema(checkpoint_database_url)
    store, claim = await current_v13_claim(database_session_factory)
    clock = TaskClock()
    replies = ThirdRoundSynthesisReplies(database_session_factory, second_route_case="follow_up")
    original = OpenHypothesisStore.save
    save_count = 0

    async def save_third_then_crash(self, *args, **kwargs):
        nonlocal save_count
        save_count += 1
        result = await original(self, *args, **kwargs)
        if save_count == 3:
            raise RuntimeError("模拟第三轮假设提交后 checkpoint 前中断")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(OpenHypothesisStore, "save", save_third_then_crash)
        async with (
            httpx2.AsyncClient(transport=httpx2.MockTransport(replies)) as client,
            open_checkpoint_saver(checkpoint_database_url) as saver,
        ):
            graph = synthesis_factory(
                database_session_factory,
                RecordingFrozenSource(claim.request),
                client,
                saver,
                clock,
            )(processor_context(claim))
            with pytest.raises(RuntimeError, match="checkpoint 前中断"):
                await graph.investigate(claim.request, thread_id=claim.run.graph_thread_id)

    before = await CallBudgetQueries(database_session_factory).get_snapshot(claim.run.id)
    hypotheses = await OpenHypothesisQueries(database_session_factory).list_for_run(claim.run.id)
    assert len(hypotheses) == 3
    await _fail_and_reclaim(store, claim, clock)
    replacement = await store.claim_next(
        worker_id="replacement-after-third-hypothesis",
        occurred_at=clock(),
        lease_duration=timedelta(seconds=30),
        supported_workflow_versions=(WorkflowVersion.V3,),
    )
    assert replacement is not None
    recovery_source = RecordingFrozenSource(replacement.request)
    async with (
        httpx2.AsyncClient(
            transport=httpx2.MockTransport(
                lambda _: pytest.fail("已提交第三轮假设恢复不应再次请求模型")
            )
        ) as client,
        open_checkpoint_saver(checkpoint_database_url) as saver,
    ):
        graph = synthesis_factory(database_session_factory, recovery_source, client, saver, clock)(
            processor_context(replacement)
        )
        result = await graph.resume(thread_id=replacement.run.graph_thread_id)

    assert result.third_round_hypothesis == hypotheses[2]
    assert await CallBudgetQueries(database_session_factory).get_snapshot(claim.run.id) == before
    assert all(not recovery_source.calls[role] for role in EvidenceKind)


async def test_third_round_synthesis_rejects_evidence_outside_third_round(
    database_session_factory,
    checkpoint_database_url,
) -> None:
    await initialize_checkpoint_schema(checkpoint_database_url)
    _, claim = await current_v13_claim(database_session_factory)
    replies = ThirdRoundSynthesisReplies(
        database_session_factory,
        second_route_case="follow_up",
        invalid_third_evidence=True,
    )
    async with (
        httpx2.AsyncClient(transport=httpx2.MockTransport(replies)) as client,
        open_checkpoint_saver(checkpoint_database_url) as saver,
    ):
        graph = synthesis_factory(
            database_session_factory,
            RecordingFrozenSource(claim.request),
            client,
            saver,
            TaskClock(),
        )(processor_context(claim))
        with pytest.raises(TaskFactConflict, match="目标轮次计划之外"):
            await graph.investigate(claim.request, thread_id=claim.run.graph_thread_id)

    hypotheses = await OpenHypothesisQueries(database_session_factory).list_for_run(claim.run.id)
    assert len(hypotheses) == 2


async def test_third_round_synthesis_call_blocks_downgrade_without_v13_run(
    database_session_factory,
    checkpoint_database_url,
) -> None:
    await initialize_checkpoint_schema(checkpoint_database_url)
    _, claim = await current_v13_claim(database_session_factory)
    replies = ThirdRoundSynthesisReplies(database_session_factory, second_route_case="follow_up")
    async with (
        httpx2.AsyncClient(transport=httpx2.MockTransport(replies)) as client,
        open_checkpoint_saver(checkpoint_database_url) as saver,
    ):
        graph = synthesis_factory(
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
            .values(graph_version="dynamic-investigation-graph-v12")
        )

    with pytest.raises(DBAPIError, match="不能删除已保存的第三轮综合调用事实"):
        await asyncio.to_thread(
            command.downgrade,
            migration_config(),
            "0054_third_round_investigation",
        )
    async with database_session_factory() as session:
        assert (
            await session.scalar(text("SELECT version_num FROM alembic_version"))
            == "0080_candidate_release_request"
        )


async def test_third_round_hypothesis_blocks_downgrade_without_v13_or_slot_two(
    database_session_factory,
    checkpoint_database_url,
) -> None:
    await initialize_checkpoint_schema(checkpoint_database_url)
    store, claim = await current_v13_claim(database_session_factory)
    clock = TaskClock()
    replies = ThirdRoundSynthesisReplies(database_session_factory, second_route_case="follow_up")
    async with (
        httpx2.AsyncClient(transport=httpx2.MockTransport(replies)) as client,
        open_checkpoint_saver(checkpoint_database_url) as saver,
    ):
        graph = synthesis_factory(
            database_session_factory,
            RecordingFrozenSource(claim.request),
            client,
            saver,
            clock,
        )(processor_context(claim))
        await graph.investigate(claim.request, thread_id=claim.run.graph_thread_id)

    hypotheses = await OpenHypothesisQueries(database_session_factory).list_for_run(claim.run.id)
    third_hypothesis = hypotheses[2]
    await _fail_and_reclaim(store, claim, clock)
    replacement = await store.claim_next(
        worker_id="replacement-for-hypothesis-migration-guard",
        occurred_at=clock(),
        lease_duration=timedelta(seconds=30),
        supported_workflow_versions=(WorkflowVersion.V3,),
    )
    assert replacement is not None

    async with database_session_factory() as session, session.begin():
        await session.execute(
            text(
                "ALTER TABLE investigation_open_hypotheses "
                "DISABLE TRIGGER trg_open_hypotheses_immutable"
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
                "idempotency_key": f"{replacement.attempt.id}:root:synthesize:1",
                "call_id": third_hypothesis.source_call_id,
            },
        )
        await session.execute(
            text(
                "UPDATE investigation_open_hypotheses "
                "SET investigation_attempt_id = :attempt_id WHERE id = :hypothesis_id"
            ),
            {
                "attempt_id": replacement.attempt.id,
                "hypothesis_id": third_hypothesis.id,
            },
        )
        await session.execute(
            text(
                "ALTER TABLE investigation_open_hypotheses "
                "ENABLE TRIGGER trg_open_hypotheses_immutable"
            )
        )
        await session.execute(
            update(InvestigationRunRow)
            .where(InvestigationRunRow.id == claim.run.id)
            .values(graph_version="dynamic-investigation-graph-v12")
        )

    with pytest.raises(DBAPIError, match="不能删除已保存的第三轮开放假设"):
        await asyncio.to_thread(
            command.downgrade,
            migration_config(),
            "0054_third_round_investigation",
        )
    async with database_session_factory() as session:
        assert (
            await session.scalar(text("SELECT version_num FROM alembic_version"))
            == "0080_candidate_release_request"
        )


async def _fail_and_reclaim(store, claim, clock) -> None:
    await store.fail(
        claim.work_item.id,
        lease_token=claim.work_item.lease_token,
        occurred_at=clock(),
        error=WorkItemError(error_code="crash", message="第三轮综合恢复窗口中断"),
        retry_delay=timedelta(0),
    )
    clock.current += timedelta(seconds=1)
