"""真实 PostgreSQL 与 checkpoint 验证第三轮最大轮次停止路由与恢复。"""

import asyncio
import json
from datetime import timedelta

import httpx2
import pytest
from sqlalchemy import text, update
from sqlalchemy.exc import DBAPIError

from alembic import command
from opsswarm.checkpoints import initialize_checkpoint_schema, open_checkpoint_saver
from opsswarm.investigation.budget.contracts import CallBudgetPolicy
from opsswarm.investigation.budget.queries import CallBudgetQueries
from opsswarm.investigation.context.prompts import THIRD_ROUND_HYPOTHESIS_REVIEW_PROMPT
from opsswarm.investigation.models import EvidenceKind, InvestigationRunStatus
from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.investigation.review_routing.persistence import ReviewRoutingStore
from opsswarm.investigation.review_routing.queries import ReviewRoutingQueries
from opsswarm.investigation.review_routing.service import ThirdRoundReviewRoutingService
from opsswarm.investigation.tasks.errors import TaskFactConflict
from opsswarm.investigation.tasks.queries import CollaborationQueries
from opsswarm.investigation.workflow_versions import WorkflowVersion
from opsswarm.worker.dynamic_graph import (
    DynamicThirdRoundReviewGraphFactory,
    DynamicThirdRoundRoutingGraphFactory,
)
from tests.integration.test_dynamic_first_round_graph import (
    RecordingFrozenSource,
    processor_context,
)
from tests.integration.test_dynamic_third_round_review_graph import ThirdRoundReviewReplies
from tests.integration.test_dynamic_third_round_synthesis_graph import (
    _fail_and_reclaim,
    migration_config,
)
from tests.loop_helpers import loop_policy
from tests.model_budget_helpers import sdk_model, sdk_response
from tests.task_facts_helpers import BASE_TIME, TaskClock, started_run

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


class ThirdRoundRoutingReplies(ThirdRoundReviewReplies):
    """前两轮固定补查，第三轮按场景返回独立审查意见。"""

    def __init__(self, db, *, third_route_case: str) -> None:
        super().__init__(db, second_route_case="follow_up")
        self.third_route_case = third_route_case

    async def __call__(self, request):
        payload = json.loads(request.content)
        data = json.loads(payload["messages"][1]["content"])
        prompt = payload["messages"][0]["content"]
        if "open_hypothesis" in data and prompt == THIRD_ROUND_HYPOTHESIS_REVIEW_PROMPT:
            self.review_prompts.append(prompt)
            self.review_inputs.append(data)
            hypothesis = data["open_hypothesis"]["hypothesis"]
            evidence_ids = hypothesis["supporting_evidence_ids"]
            proposal = {
                "support_is_sufficient": True,
                "rationale": "第三轮支持材料足以进入确定性候选评估。",
                "accepted_supporting_evidence_ids": evidence_ids[:1],
                "counter_evidence_ids": [],
                "evidence_gaps": [],
                "competing_explanations": [],
                "suggested_direction": "evaluate_for_routing",
                "follow_up_suggestions": [],
            }
            if self.third_route_case == "objection":
                proposal.update(
                    rationale="第三轮仍保留需要人工判断的证据缺口。",
                    evidence_gaps=["仍需人工核对最终影响范围"],
                )
            elif self.third_route_case == "round_limit":
                proposal.update(
                    support_is_sufficient=False,
                    rationale="第三轮仍需日志补查，但已达到冻结轮次上限。",
                    evidence_gaps=["需要更长时间窗的日志证据"],
                    suggested_direction="follow_up",
                    follow_up_suggestions=[
                        {
                            "role": "log",
                            "question": "扩大日志时间窗后是否仍存在相同错误模式？",
                            "rationale": "现有证据不足以排除瞬时异常。",
                        }
                    ],
                )
            return sdk_response(payload, proposal)
        return await super().__call__(request)


async def current_v15_claim(database_session_factory):
    store, _ = await started_run(
        database_session_factory,
        workflow_version=WorkflowVersion.V3,
        call_budget=CallBudgetPolicy(max_model_calls=13),
        model_budget=loop_policy(),
    )
    claim = await store.claim_next(
        worker_id="dynamic-v15-third-round-routing-worker",
        occurred_at=BASE_TIME + timedelta(seconds=2),
        lease_duration=timedelta(seconds=30),
        supported_workflow_versions=(WorkflowVersion.V3,),
    )
    assert claim is not None
    assert claim.run.graph_version == "dynamic-investigation-graph-v15"
    return store, claim


def routing_factory(db, source, client, saver, clock):
    return DynamicThirdRoundRoutingGraphFactory(
        session_factory=db,
        data_source=source,
        model=sdk_model(client),
        checkpointer=saver,
        clock=clock,
    )


@pytest.mark.parametrize(
    ("third_route_case", "expected_route", "expected_reason"),
    [
        ("candidate", "candidate_evaluation", "candidate_requirements_satisfied"),
        ("objection", "await_human", "evidence_gaps_present"),
        ("round_limit", "await_human", "round_limit_reached"),
    ],
)
async def test_third_round_review_is_routed_to_bounded_terminal_decision(
    database_session_factory,
    checkpoint_database_url,
    third_route_case,
    expected_route,
    expected_reason,
) -> None:
    await initialize_checkpoint_schema(checkpoint_database_url)
    store, claim = await current_v15_claim(database_session_factory)
    source = RecordingFrozenSource(claim.request)
    replies = ThirdRoundRoutingReplies(
        database_session_factory,
        third_route_case=third_route_case,
    )
    async with (
        httpx2.AsyncClient(transport=httpx2.MockTransport(replies)) as client,
        open_checkpoint_saver(checkpoint_database_url) as saver,
    ):
        graph = routing_factory(database_session_factory, source, client, saver, TaskClock())(
            processor_context(claim)
        )
        result = await graph.investigate(claim.request, thread_id=claim.run.graph_thread_id)
        assert await graph.resume(thread_id=claim.run.graph_thread_id) == result
        checkpoint = await saver.aget_tuple(
            {"configurable": {"thread_id": claim.run.graph_thread_id}}
        )

    routes = await ReviewRoutingQueries(database_session_factory).list_for_run(claim.run.id)
    snapshot = await CollaborationQueries(database_session_factory).get_snapshot(claim.run.id)
    budget = await CallBudgetQueries(database_session_factory).get_snapshot(claim.run.id)
    assert len(routes) == 3
    assert [item.decision.round_number for item in routes] == [1, 2, 3]
    assert len(snapshot.rounds) == 3
    assert routes[2] == result.third_round_review_route
    assert routes[2].open_hypothesis_id == result.third_round_hypothesis.id
    assert routes[2].hypothesis_review_id == result.third_round_hypothesis_review.id
    assert routes[2].decision.route == expected_route
    assert expected_reason in routes[2].decision.reason_codes
    assert routes[2].decision.round_number == routes[2].decision.max_rounds == 3
    assert routes[2].decision.remaining_rounds == 0
    assert routes[2].decision.existing_task_count == sum(
        len(round_record.tasks) for round_record in snapshot.rounds
    )
    assert routes[2].decision.route != "follow_up"
    if third_route_case == "round_limit":
        assert routes[2].decision.follow_up_suggestions
        assert "follow_up_capacity_available" not in routes[2].decision.reason_codes
    assert budget.reserved_model_calls == 13
    assert budget.reserved_tool_calls == 6
    assert checkpoint is not None
    assert checkpoint.metadata["graph_version"] == "dynamic-investigation-graph-v15"
    run = await store.get_run(claim.run.id)
    assert run.status is InvestigationRunStatus.RUNNING and run.result is None


@pytest.mark.parametrize("second_route_case", ["human", "candidate"])
async def test_second_round_terminal_route_does_not_create_third_round_route(
    database_session_factory,
    checkpoint_database_url,
    second_route_case,
) -> None:
    await initialize_checkpoint_schema(checkpoint_database_url)
    store, claim = await current_v15_claim(database_session_factory)
    replies = ThirdRoundReviewReplies(
        database_session_factory,
        second_route_case=second_route_case,
    )
    async with (
        httpx2.AsyncClient(transport=httpx2.MockTransport(replies)) as client,
        open_checkpoint_saver(checkpoint_database_url) as saver,
    ):
        graph = routing_factory(
            database_session_factory,
            RecordingFrozenSource(claim.request),
            client,
            saver,
            TaskClock(),
        )(processor_context(claim))
        result = await graph.investigate(claim.request, thread_id=claim.run.graph_thread_id)

    routes = await ReviewRoutingQueries(database_session_factory).list_for_run(claim.run.id)
    assert len(routes) == 2
    assert result.third_round_plan is None
    assert result.third_round_review_route is None
    run = await store.get_run(claim.run.id)
    assert run.status is InvestigationRunStatus.RUNNING and run.result is None


@pytest.mark.parametrize("crash_after_save", [False, True])
async def test_third_round_routing_recovers_across_fact_commit_window(
    database_session_factory,
    checkpoint_database_url,
    monkeypatch,
    crash_after_save,
) -> None:
    await initialize_checkpoint_schema(checkpoint_database_url)
    store, claim = await current_v15_claim(database_session_factory)
    clock = TaskClock()
    replies = ThirdRoundRoutingReplies(database_session_factory, third_route_case="candidate")
    original = ReviewRoutingStore.save
    save_count = 0

    async def interrupt_third_save(self, *args, **kwargs):
        nonlocal save_count
        save_count += 1
        if save_count == 3 and not crash_after_save:
            raise RuntimeError("模拟第三轮路由事实提交前中断")
        saved = await original(self, *args, **kwargs)
        if save_count == 3 and crash_after_save:
            raise RuntimeError("模拟第三轮路由事实提交后 checkpoint 前中断")
        return saved

    with monkeypatch.context() as patch:
        patch.setattr(ReviewRoutingStore, "save", interrupt_third_save)
        async with (
            httpx2.AsyncClient(transport=httpx2.MockTransport(replies)) as client,
            open_checkpoint_saver(checkpoint_database_url) as saver,
        ):
            graph = routing_factory(
                database_session_factory,
                RecordingFrozenSource(claim.request),
                client,
                saver,
                clock,
            )(processor_context(claim))
            with pytest.raises(RuntimeError, match="第三轮路由事实提交"):
                await graph.investigate(claim.request, thread_id=claim.run.graph_thread_id)

    before = await CallBudgetQueries(database_session_factory).get_snapshot(claim.run.id)
    routes_before = await ReviewRoutingQueries(database_session_factory).list_for_run(claim.run.id)
    assert len(routes_before) == (3 if crash_after_save else 2)
    await _fail_and_reclaim(store, claim, clock)
    replacement = await store.claim_next(
        worker_id="dynamic-v15-third-round-routing-recovery",
        occurred_at=clock(),
        lease_duration=timedelta(seconds=30),
        supported_workflow_versions=(WorkflowVersion.V3,),
    )
    assert replacement is not None
    recovery_source = RecordingFrozenSource(replacement.request)
    async with (
        httpx2.AsyncClient(
            transport=httpx2.MockTransport(lambda _: pytest.fail("确定性路由恢复不应再次调用模型"))
        ) as client,
        open_checkpoint_saver(checkpoint_database_url) as saver,
    ):
        graph = routing_factory(
            database_session_factory,
            recovery_source,
            client,
            saver,
            clock,
        )(processor_context(replacement))
        result = await graph.resume(thread_id=replacement.run.graph_thread_id)

    routes = await ReviewRoutingQueries(database_session_factory).list_for_run(claim.run.id)
    assert len(routes) == 3
    assert result.third_round_review_route == routes[2]
    assert await CallBudgetQueries(database_session_factory).get_snapshot(claim.run.id) == before
    assert all(not recovery_source.calls[role] for role in EvidenceKind)


async def test_v14_cannot_save_third_round_route_outside_graph(
    database_session_factory,
    checkpoint_database_url,
) -> None:
    await initialize_checkpoint_schema(checkpoint_database_url)
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
        worker_id="dynamic-v14-direct-routing-rejection",
        occurred_at=BASE_TIME + timedelta(seconds=2),
        lease_duration=timedelta(seconds=30),
        supported_workflow_versions=(WorkflowVersion.V3,),
    )
    assert claim is not None
    replies = ThirdRoundReviewReplies(database_session_factory, second_route_case="follow_up")
    clock = TaskClock()
    async with (
        httpx2.AsyncClient(transport=httpx2.MockTransport(replies)) as client,
        open_checkpoint_saver(checkpoint_database_url) as saver,
    ):
        result = await DynamicThirdRoundReviewGraphFactory(
            session_factory=database_session_factory,
            data_source=RecordingFrozenSource(claim.request),
            model=sdk_model(client),
            checkpointer=saver,
            clock=clock,
        )(processor_context(claim)).investigate(
            claim.request,
            thread_id=claim.run.graph_thread_id,
        )

    with pytest.raises(TaskFactConflict) as caught:
        await ThirdRoundReviewRoutingService(
            database_session_factory,
            run_id=claim.run.id,
            lease_token=claim.work_item.lease_token,
            clock=clock,
        ).route(result)
    assert caught.value.code == "review_routing_not_supported"
    assert len(await ReviewRoutingQueries(database_session_factory).list_for_run(claim.run.id)) == 2
    try:
        await asyncio.to_thread(
            command.downgrade,
            migration_config(),
            "0056_third_round_review",
        )
        async with database_session_factory() as session:
            assert await session.scalar(text("SELECT version_num FROM alembic_version")) == (
                "0056_third_round_review"
            )
    finally:
        await asyncio.to_thread(command.upgrade, migration_config(), "head")


async def test_third_round_route_fact_blocks_downgrade_without_v15_run(
    database_session_factory,
    checkpoint_database_url,
) -> None:
    await initialize_checkpoint_schema(checkpoint_database_url)
    _, claim = await current_v15_claim(database_session_factory)
    replies = ThirdRoundRoutingReplies(database_session_factory, third_route_case="candidate")
    async with (
        httpx2.AsyncClient(transport=httpx2.MockTransport(replies)) as client,
        open_checkpoint_saver(checkpoint_database_url) as saver,
    ):
        await routing_factory(
            database_session_factory,
            RecordingFrozenSource(claim.request),
            client,
            saver,
            TaskClock(),
        )(processor_context(claim)).investigate(
            claim.request,
            thread_id=claim.run.graph_thread_id,
        )

    async with database_session_factory() as session, session.begin():
        await session.execute(
            update(InvestigationRunRow)
            .where(InvestigationRunRow.id == claim.run.id)
            .values(graph_version="dynamic-investigation-graph-v14")
        )

    with pytest.raises(DBAPIError, match="不能删除已保存的第三轮最大轮次停止路由事实"):
        await asyncio.to_thread(
            command.downgrade,
            migration_config(),
            "0056_third_round_review",
        )
    async with database_session_factory() as session:
        assert await session.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0070_human_input_reassessment"
        )
