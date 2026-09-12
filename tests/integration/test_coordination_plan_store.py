"""真实 PostgreSQL 验证动态协调计划、策略和业务轮次原子绑定。"""

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import DBAPIError

from opsswarm.investigation.coordination.contracts import (
    CoordinationPolicy,
    CoordinationProposal,
    ProposedTask,
)
from opsswarm.investigation.coordination.persistence import CoordinationPlanStore
from opsswarm.investigation.coordination.queries import CoordinationPlanQueries
from opsswarm.investigation.coordination.tables import (
    InvestigationCoordinationPlanRow,
    InvestigationCoordinationPolicyRow,
)
from opsswarm.investigation.tasks.artifact_tables import AgentTaskEventRow
from opsswarm.investigation.tasks.contracts import TaskOutcome
from opsswarm.investigation.tasks.errors import TaskFactConflict
from opsswarm.investigation.tasks.queries import CollaborationQueries
from opsswarm.investigation.tasks.store import InvestigationTaskStore
from opsswarm.investigation.tasks.tables import AgentTaskRow, InvestigationRoundRow
from tests.task_facts_helpers import BASE_TIME, TaskClock, assignment, claimed_run

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def proposal(*, round_number: int = 1, goal: str = "核对关键日志和变更") -> CoordinationProposal:
    return CoordinationProposal(
        round_number=round_number,
        goal=goal,
        tasks=(
            ProposedTask(
                task_key="log",
                role="log",
                goal="检查异常日志模式",
                lookback_seconds=600,
            ),
            ProposedTask(
                task_key="change",
                role="change",
                goal="核对近期发布",
                lookback_seconds=3600,
            ),
        ),
    )


def create_args(claim, **changes):
    return {
        "lease_token": claim.work_item.lease_token,
        "policy": changes.pop("policy", CoordinationPolicy()),
        "proposal": changes.pop("proposal", proposal()),
        "observation_cutoff_at": changes.pop("observation_cutoff_at", BASE_TIME),
        "evidence_snapshot_id": changes.pop("evidence_snapshot_id", "dynamic-snapshot-v1"),
        **changes,
    }


async def test_plan_creation_is_atomic_queryable_and_idempotent_under_race(
    database_session_factory,
):
    _, claim = await claimed_run(database_session_factory)
    store = CoordinationPlanStore(database_session_factory, clock=TaskClock())
    results = await asyncio.gather(
        *(store.create_plan(claim.run.id, **create_args(claim)) for _ in range(4))
    )
    assert sum(created for _, created in results) == 1
    assert len({record.round_id for record, _ in results}) == 1
    record = results[0][0]
    assert record.investigation_attempt_id == claim.attempt.id
    assert record.plan.assignment.tasks[0].service_name == "order-service"

    queries = CoordinationPlanQueries(database_session_factory)
    policy = await queries.get_policy(claim.run.id)
    plans = await queries.list_plans(claim.run.id)
    snapshot = await CollaborationQueries(database_session_factory).get_snapshot(claim.run.id)
    assert policy is not None and policy.policy == CoordinationPolicy()
    assert plans == (record,)
    assert [task.assignment.role for task in snapshot.rounds[0].tasks] == ["change", "log"]
    async with database_session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(InvestigationRoundRow)) == 1
        assert await session.scalar(select(func.count()).select_from(AgentTaskRow)) == 2


async def test_plan_replay_rejects_policy_content_and_fixed_history_conflicts(
    database_session_factory,
):
    _, claim = await claimed_run(database_session_factory)
    store = CoordinationPlanStore(database_session_factory, clock=TaskClock())
    await store.create_plan(claim.run.id, **create_args(claim))
    for changes, code in (
        ({"policy": CoordinationPolicy(max_total_tasks=5)}, "coordination_policy_conflict"),
        ({"proposal": proposal(goal="另一份计划")}, "coordination_plan_conflict"),
    ):
        with pytest.raises(TaskFactConflict) as caught:
            await store.create_plan(claim.run.id, **create_args(claim, **changes))
        assert caught.value.code == code

    _, fixed_claim = await claimed_run(database_session_factory)
    await InvestigationTaskStore(database_session_factory, clock=TaskClock()).create_round(
        fixed_claim.run.id,
        lease_token=fixed_claim.work_item.lease_token,
        assignment=assignment(),
    )
    with pytest.raises(TaskFactConflict) as caught:
        await store.create_plan(fixed_claim.run.id, **create_args(fixed_claim))
    assert caught.value.code == "coordination_history_conflict"


async def test_next_round_reuses_policy_and_counts_prior_tasks(database_session_factory):
    _, claim = await claimed_run(database_session_factory)
    clock = TaskClock()
    plans = CoordinationPlanStore(database_session_factory, clock=clock)
    tasks = InvestigationTaskStore(database_session_factory, clock=clock)
    await plans.create_plan(claim.run.id, **create_args(claim))
    first_round = (
        await CollaborationQueries(database_session_factory).get_snapshot(claim.run.id)
    ).rounds[0]
    for task in first_round.tasks:
        started = await tasks.begin_attempt(
            claim.run.id,
            task.id,
            lease_token=claim.work_item.lease_token,
            expected_version=1,
        )
        await tasks.finish_attempt(
            claim.run.id,
            task.id,
            started.attempt.id,
            lease_token=claim.work_item.lease_token,
            outcome=TaskOutcome(status="failed", error_code="no_relevant_evidence"),
        )
    second, created = await plans.create_plan(
        claim.run.id,
        **create_args(claim, proposal=proposal(round_number=2, goal="补查下一轮")),
    )
    assert created is True and second.plan.assignment.round_number == 2
    saved = await CoordinationPlanQueries(database_session_factory).list_plans(claim.run.id)
    assert [item.plan.assignment.round_number for item in saved] == [1, 2]


async def test_invalid_or_expired_creation_leaves_no_coordination_facts(
    database_session_factory,
):
    _, claim = await claimed_run(database_session_factory)
    clock = TaskClock()
    store = CoordinationPlanStore(database_session_factory, clock=clock)
    clock.current += timedelta(minutes=1)
    with pytest.raises(TaskFactConflict) as caught:
        await store.create_plan(claim.run.id, **create_args(claim))
    assert caught.value.code == "task_lease_lost"
    async with database_session_factory() as session:
        for table in (
            InvestigationCoordinationPolicyRow,
            InvestigationCoordinationPlanRow,
            InvestigationRoundRow,
            AgentTaskRow,
        ):
            assert await session.scalar(select(func.count()).select_from(table)) == 0


async def test_lease_expiring_before_commit_rolls_back_all_facts(database_session_factory):
    _, claim = await claimed_run(database_session_factory)

    class ExitExpiryClock:
        calls = 0

        def __call__(self):
            self.calls += 1
            return BASE_TIME + timedelta(seconds=3 if self.calls == 1 else 33)

    store = CoordinationPlanStore(database_session_factory, clock=ExitExpiryClock())
    with pytest.raises(TaskFactConflict) as caught:
        await store.create_plan(claim.run.id, **create_args(claim))
    assert caught.value.code == "task_lease_lost"
    async with database_session_factory() as session:
        for table in (
            InvestigationCoordinationPolicyRow,
            InvestigationCoordinationPlanRow,
            InvestigationRoundRow,
            AgentTaskRow,
            AgentTaskEventRow,
        ):
            assert await session.scalar(select(func.count()).select_from(table)) == 0


async def test_database_rejects_mutating_coordination_facts(database_session_factory):
    _, claim = await claimed_run(database_session_factory)
    await CoordinationPlanStore(database_session_factory, clock=TaskClock()).create_plan(
        claim.run.id, **create_args(claim)
    )
    for statement in (
        update(InvestigationCoordinationPolicyRow).values(content_sha256="0" * 64),
        delete(InvestigationCoordinationPolicyRow),
        update(InvestigationCoordinationPlanRow).values(content_sha256="0" * 64),
        delete(InvestigationCoordinationPlanRow),
    ):
        with pytest.raises(DBAPIError, match="不可更新或删除"):
            async with database_session_factory() as session, session.begin():
                await session.execute(statement)


async def test_query_rejects_changed_round_source(database_session_factory):
    _, claim = await claimed_run(database_session_factory)
    await CoordinationPlanStore(database_session_factory, clock=TaskClock()).create_plan(
        claim.run.id, **create_args(claim)
    )
    async with database_session_factory() as session, session.begin():
        await session.execute(update(InvestigationRoundRow).values(content_hash="0" * 64))
    with pytest.raises(TaskFactConflict) as caught:
        await CoordinationPlanQueries(database_session_factory).list_plans(claim.run.id)
    assert caught.value.code == "coordination_plan_integrity_violation"


async def test_query_rejects_changed_task_execution_limit(database_session_factory):
    _, claim = await claimed_run(database_session_factory)
    await CoordinationPlanStore(database_session_factory, clock=TaskClock()).create_plan(
        claim.run.id, **create_args(claim)
    )
    async with database_session_factory() as session, session.begin():
        await session.execute(update(AgentTaskRow).values(max_attempts=2))
    with pytest.raises(TaskFactConflict) as caught:
        await CoordinationPlanQueries(database_session_factory).list_plans(claim.run.id)
    assert caught.value.code == "coordination_plan_integrity_violation"


async def test_fixed_run_has_no_dynamic_policy_or_plans(database_session_factory):
    _, claim = await claimed_run(database_session_factory)
    queries = CoordinationPlanQueries(database_session_factory)
    assert await queries.get_policy(claim.run.id) is None
    assert await queries.list_plans(claim.run.id) == ()
