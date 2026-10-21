"""真实 PostgreSQL 验证来源核验作用域的复用、修订戳和失效边界。"""

import asyncio
import json
from contextlib import suppress
from datetime import timedelta
from uuid import UUID, uuid4

import httpx2
import pytest
from pydantic import BaseModel
from sqlalchemy import select, text, update

from opsswarm.identity.tables import OperatorAuthorizationRow
from opsswarm.incidents.persistence.tables import IncidentRow
from opsswarm.investigation.budget.tables import CallReservationRow
from opsswarm.investigation.coordination.persistence import CoordinationPlanStore
from opsswarm.investigation.coordination.tables import InvestigationCoordinationPlanRow
from opsswarm.investigation.persistence.source_revision import read_source_stamp
from opsswarm.investigation.persistence.source_validation import reuse_validated_source
from opsswarm.investigation.tasks.errors import TaskFactConflict
from opsswarm.investigation.tasks.tables import AgentTaskRow
from opsswarm.investigation.workflow_versions import WorkflowVersion
from tests.api.test_coordination_experience_api import (
    arguments,
    planner,
    start,
)
from tests.api.test_experience_lifecycle_api import (
    experience_context as experience_fixture,
)
from tests.api.test_experience_lifecycle_api import history
from tests.integration.test_coordination_plan_store import create_args
from tests.integration.test_coordination_planner import proposal
from tests.loop_helpers import loop_policy
from tests.model_budget_helpers import sdk_response
from tests.task_facts_helpers import BASE_TIME, TaskClock, claimed_run

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
experience_context = experience_fixture


class SourceView(BaseModel):
    """保留可变嵌套材料，用来验证缓存结果不与调用方共享引用。"""

    run_id: UUID
    payload: dict[str, list[str]]


async def saved_plan(factory):
    _, claim = await claimed_run(
        factory,
        workflow_version=WorkflowVersion.V3,
        model_budget=loop_policy(),
        supported_workflow_versions=(WorkflowVersion.V3,),
    )
    record, created = await CoordinationPlanStore(factory, clock=TaskClock()).create_plan(
        claim.run.id, **create_args(claim)
    )
    assert created
    return claim, record


def source_view(row):
    return SourceView(
        run_id=row.investigation_run_id,
        payload={"goals": [row.plan["proposal"]["goal"]]},
    )


async def test_diamond_rebuilds_shared_source_once_and_returns_independent_dtos(
    database_session_factory,
):
    _, record = await saved_plan(database_session_factory)
    calls = {"leaf": 0, "left": 0, "right": 0, "root": 0}

    @reuse_validated_source
    async def leaf(session, row):
        calls["leaf"] += 1
        return source_view(row)

    @reuse_validated_source
    async def left(session, row):
        calls["left"] += 1
        value = await leaf(session, row)
        value.payload["goals"].append("只属于左分支")
        return value

    @reuse_validated_source
    async def right(session, row):
        calls["right"] += 1
        value = await leaf(session, row)
        assert value.payload["goals"] == [row.plan["proposal"]["goal"]]
        return value

    @reuse_validated_source
    async def root(session, row):
        calls["root"] += 1
        await left(session, row)
        return await right(session, row)

    async with database_session_factory() as session, session.begin():
        row = await session.get(InvestigationCoordinationPlanRow, record.round_id)
        before = dict(session.info)
        first = await root(session, row)
        assert calls == {"leaf": 1, "left": 1, "right": 1, "root": 1}
        assert session.info == before
        first.payload["goals"].append("最外层返回后的修改")
        second = await root(session, row)
        assert second.payload["goals"] == [row.plan["proposal"]["goal"]]
        assert calls == {"leaf": 2, "left": 2, "right": 2, "root": 2}
        assert session.info == before


async def test_scope_never_survives_root_transaction_or_session(database_session_factory):
    _, record = await saved_plan(database_session_factory)
    calls = 0

    @reuse_validated_source
    async def leaf(session, row):
        nonlocal calls
        calls += 1
        return source_view(row)

    @reuse_validated_source
    async def root(session, row):
        await leaf(session, row)
        return await leaf(session, row)

    async with database_session_factory() as session:
        async with session.begin():
            row = await session.get(InvestigationCoordinationPlanRow, record.round_id)
            await root(session, row)
        async with session.begin():
            await root(session, row)
    async with database_session_factory() as session, session.begin():
        row = await session.get(InvestigationCoordinationPlanRow, record.round_id)
        await root(session, row)
    assert calls == 3


async def test_nested_validation_cannot_change_run_scope(database_session_factory):
    _, record = await saved_plan(database_session_factory)
    _, other_record = await saved_plan(database_session_factory)

    @reuse_validated_source
    async def leaf(session, row):
        return source_view(row)

    @reuse_validated_source
    async def root(session, row):
        await leaf(session, row)
        other = await session.get(InvestigationCoordinationPlanRow, other_record.round_id)
        return await leaf(session, other)

    async with database_session_factory() as session, session.begin():
        row = await session.get(InvestigationCoordinationPlanRow, record.round_id)
        with pytest.raises(TaskFactConflict) as caught:
            await root(session, row)
        assert caught.value.code == "source_validation_changed"


async def test_nested_validation_cannot_use_scope_from_another_task(database_session_factory):
    _, record = await saved_plan(database_session_factory)

    @reuse_validated_source
    async def leaf(session, row):
        return source_view(row)

    @reuse_validated_source
    async def root(session, row):
        await leaf(session, row)
        return await asyncio.create_task(leaf(session, row))

    async with database_session_factory() as session, session.begin():
        row = await session.get(InvestigationCoordinationPlanRow, record.round_id)
        with pytest.raises(TaskFactConflict) as caught:
            await root(session, row)
        assert caught.value.code == "source_validation_changed"


async def test_stamp_tracks_task_membership_and_ignores_other_runs(database_session_factory):
    claim, _ = await saved_plan(database_session_factory)
    other_claim, _ = await saved_plan(database_session_factory)
    async with database_session_factory() as session, session.begin():
        before = await read_source_stamp(session, claim.run.id)
        async with database_session_factory() as writer, writer.begin():
            await writer.execute(
                update(IncidentRow)
                .where(IncidentRow.id == other_claim.run.incident_id)
                .values(title="不属于本次来源范围的事故")
            )
        assert await read_source_stamp(session, claim.run.id) == before
        extra_id = uuid4()
        await session.execute(
            text(
                "INSERT INTO agent_tasks "
                "(id, investigation_run_id, round_id, task_key, assignment, status, "
                "version, attempt_count, max_attempts, created_at, updated_at, completed_at) "
                "SELECT :id, investigation_run_id, round_id, 'extra', assignment, status, "
                "version, attempt_count, max_attempts, created_at, updated_at, completed_at "
                "FROM agent_tasks WHERE investigation_run_id = :run_id LIMIT 1"
            ),
            {"id": extra_id, "run_id": claim.run.id},
        )
        assert await read_source_stamp(session, claim.run.id) != before
        await session.execute(text("DELETE FROM agent_tasks WHERE id = :id"), {"id": extra_id})
        assert await read_source_stamp(session, claim.run.id) == before


async def test_same_transaction_second_sql_update_changes_stamp_without_new_xmin(
    database_session_factory,
):
    claim, _ = await saved_plan(database_session_factory)
    async with database_session_factory() as session, session.begin():
        stamps = [await read_source_stamp(session, claim.run.id)]
        row_versions = []
        for title in ("第一次事务内更新", "第二次事务内更新"):
            await session.execute(
                text("UPDATE incidents SET title = :title WHERE id = :incident_id"),
                {"title": title, "incident_id": claim.run.incident_id},
            )
            row_versions.append(
                (
                    await session.execute(
                        text("SELECT xmin::text, ctid::text FROM incidents WHERE id = :id"),
                        {"id": claim.run.incident_id},
                    )
                ).one()
            )
            stamps.append(await read_source_stamp(session, claim.run.id))
        assert row_versions[0][0] == row_versions[1][0]
        assert row_versions[0][1] != row_versions[1][1]
        assert len(set(stamps)) == 3


async def test_other_session_task_change_invalidates_cached_ancestor(database_session_factory):
    claim, record = await saved_plan(database_session_factory)
    calls = 0

    @reuse_validated_source
    async def leaf(session, row):
        nonlocal calls
        calls += 1
        return source_view(row)

    @reuse_validated_source
    async def root(session, row):
        await leaf(session, row)
        async with database_session_factory() as writer, writer.begin():
            await writer.execute(
                update(AgentTaskRow)
                .where(AgentTaskRow.investigation_run_id == claim.run.id)
                .values(max_attempts=2)
            )
        return await leaf(session, row)

    async with database_session_factory() as session, session.begin():
        row = await session.get(InvestigationCoordinationPlanRow, record.round_id)
        before = dict(session.info)
        with pytest.raises(TaskFactConflict) as caught:
            await root(session, row)
        assert caught.value.code == "source_validation_changed"
        assert calls == 1
        assert session.info == before


async def test_scope_rejects_raw_sql_write_before_reusing_source(database_session_factory):
    claim, record = await saved_plan(database_session_factory)

    @reuse_validated_source
    async def leaf(session, row):
        return source_view(row)

    @reuse_validated_source
    async def root(session, row):
        await leaf(session, row)
        await session.execute(
            text("UPDATE incidents SET title = :title WHERE id = :id"),
            {"title": "核验期间不允许写来源", "id": claim.run.incident_id},
        )
        return await leaf(session, row)

    async with database_session_factory() as session, session.begin():
        row = await session.get(InvestigationCoordinationPlanRow, record.round_id)
        with pytest.raises(TaskFactConflict) as caught:
            await root(session, row)
        assert caught.value.code == "source_validation_changed"


@pytest.mark.parametrize("rollback", [False, True])
async def test_savepoint_boundary_invalidates_outer_scope(database_session_factory, rollback):
    _, record = await saved_plan(database_session_factory)

    @reuse_validated_source
    async def leaf(session, row):
        return source_view(row)

    @reuse_validated_source
    async def root(session, row):
        await leaf(session, row)
        nested = await session.begin_nested()
        if rollback:
            await nested.rollback()
        else:
            await nested.commit()
        return await leaf(session, row)

    async with database_session_factory() as session, session.begin():
        row = await session.get(InvestigationCoordinationPlanRow, record.round_id)
        with pytest.raises(TaskFactConflict) as caught:
            await root(session, row)
        assert caught.value.code == "source_validation_changed"


async def test_stale_preloaded_identity_map_is_rejected_before_first_rebuild(
    database_session_factory,
):
    claim, record = await saved_plan(database_session_factory)
    calls = 0

    @reuse_validated_source
    async def root(session, row):
        nonlocal calls
        calls += 1
        return source_view(row)

    async with database_session_factory() as session, session.begin():
        row = await session.get(InvestigationCoordinationPlanRow, record.round_id)
        incident = await session.get(IncidentRow, claim.run.incident_id)
        original_title = incident.title
        async with database_session_factory() as writer, writer.begin():
            await writer.execute(
                update(IncidentRow)
                .where(IncidentRow.id == incident.id)
                .values(title="数据库已更新的事故标题")
            )
        assert incident.title == original_title
        with pytest.raises(TaskFactConflict) as caught:
            await root(session, row)
        assert caught.value.code == "source_validation_changed"
        assert calls == 0


@pytest.mark.parametrize("before_scope", [False, True])
async def test_in_place_json_edit_cannot_bypass_orm_dirty_check(
    database_session_factory, before_scope
):
    _, record = await saved_plan(database_session_factory)

    @reuse_validated_source
    async def leaf(session, row):
        return source_view(row)

    @reuse_validated_source
    async def root(session, row):
        await leaf(session, row)
        row.plan["proposal"]["goal"] = "只改内存中的嵌套 JSON"
        assert row not in session.dirty
        return await leaf(session, row)

    async with database_session_factory() as session, session.begin():
        row = await session.get(InvestigationCoordinationPlanRow, record.round_id)
        if before_scope:
            row.plan["proposal"]["goal"] = "作用域开始前的嵌套 JSON 篡改"
            assert row not in session.dirty
        with pytest.raises(TaskFactConflict) as caught:
            await (leaf(session, row) if before_scope else root(session, row))
        assert caught.value.code == "source_validation_changed"


@pytest.mark.parametrize("pending", ["dirty", "new", "deleted"])
async def test_pending_orm_changes_keep_previous_uncached_behavior(
    database_session_factory, pending
):
    claim, record = await saved_plan(database_session_factory)
    calls = 0

    @reuse_validated_source
    async def leaf(session, row):
        nonlocal calls
        calls += 1
        return source_view(row)

    @reuse_validated_source
    async def root(session, row):
        await leaf(session, row)
        return await leaf(session, row)

    async with database_session_factory() as session:
        row = await session.get(InvestigationCoordinationPlanRow, record.round_id)
        if pending == "dirty":
            incident = await session.get(IncidentRow, claim.run.incident_id)
            incident.title = "沿用带待提交变化的旧核验路径"
        elif pending == "new":
            # 不完整的新对象只用于标记 pending，禁止在本测试中自动 flush。
            session.add(IncidentRow(id=uuid4()))
        else:
            task = await session.scalar(
                select(AgentTaskRow).where(AgentTaskRow.round_id == record.round_id)
            )
            await session.delete(task)
        before = dict(session.info)
        with session.no_autoflush:
            await root(session, row)
        assert calls == 2
        assert session.info == before
        await session.rollback()


async def test_failed_validation_does_not_leave_cached_result(database_session_factory):
    _, record = await saved_plan(database_session_factory)
    calls = 0

    @reuse_validated_source
    async def leaf(session, row):
        nonlocal calls
        calls += 1
        return source_view(row)

    @reuse_validated_source
    async def root(session, row):
        await leaf(session, row)
        if calls == 1:
            raise ValueError("模拟来源核验后续失败")
        return await leaf(session, row)

    async with database_session_factory() as session, session.begin():
        row = await session.get(InvestigationCoordinationPlanRow, record.round_id)
        before = dict(session.info)
        with pytest.raises(ValueError, match="模拟来源核验后续失败"):
            await root(session, row)
        assert session.info == before
        await root(session, row)
        assert calls == 2


async def test_cancelled_validation_cleans_scope_for_next_owner_task(database_session_factory):
    _, record = await saved_plan(database_session_factory)
    reached = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    @reuse_validated_source
    async def leaf(session, row):
        nonlocal calls
        calls += 1
        return source_view(row)

    @reuse_validated_source
    async def root(session, row):
        await leaf(session, row)
        reached.set()
        await release.wait()
        return await leaf(session, row)

    async with database_session_factory() as session, session.begin():
        row = await session.get(InvestigationCoordinationPlanRow, record.round_id)
        before = dict(session.info)
        running = asyncio.create_task(root(session, row))
        try:
            await asyncio.wait_for(reached.wait(), timeout=10)
        finally:
            running.cancel()
            with pytest.raises(asyncio.CancelledError):
                await running
        assert session.info == before
        release.set()
        await root(session, row)
        assert calls == 2
        assert session.info == before


async def test_frozen_experience_authorization_change_is_included_in_source_stamp(
    experience_context, database_session_factory
):
    await history(experience_context, database_session_factory)
    _, claim = await start(experience_context, database_session_factory)
    clock = experience_context[2]
    async with httpx2.AsyncClient(
        transport=httpx2.MockTransport(
            lambda request: sdk_response(
                json.loads(request.content), proposal().model_dump(mode="json")
            )
        )
    ) as model:
        await planner(database_session_factory, claim, clock, model).create_first_plan(
            **arguments()
        )
    async with database_session_factory() as session, session.begin():
        before = await read_source_stamp(session, claim.run.id)
        async with database_session_factory() as writer, writer.begin():
            changed = await writer.execute(
                update(OperatorAuthorizationRow)
                .where(OperatorAuthorizationRow.operation == "review_experience_candidate")
                .values(role="viewer")
            )
            assert changed.rowcount == 1
        after = await read_source_stamp(session, claim.run.id)
        assert before != after


async def test_new_successful_call_cannot_hide_behind_a_memo_hit(database_session_factory):
    claim, record = await saved_plan(database_session_factory)

    @reuse_validated_source
    async def leaf(session, row):
        return source_view(row)

    @reuse_validated_source
    async def root(session, row):
        await leaf(session, row)
        async with database_session_factory() as writer, writer.begin():
            writer.add(
                CallReservationRow(
                    id=uuid4(),
                    investigation_run_id=claim.run.id,
                    investigation_attempt_id=claim.attempt.id,
                    sequence=1,
                    invocation_index=0,
                    idempotency_key=f"{claim.attempt.id}:root:synthesize",
                    operation="synthesize",
                    kind="model",
                    status="succeeded",
                    reserved_at=BASE_TIME + timedelta(seconds=3),
                    finished_at=BASE_TIME + timedelta(seconds=4),
                )
            )
        return await leaf(session, row)

    async with database_session_factory() as session, session.begin():
        row = await session.get(InvestigationCoordinationPlanRow, record.round_id)
        with pytest.raises(TaskFactConflict) as caught:
            await root(session, row)
        assert caught.value.code == "source_validation_changed"


@pytest.mark.parametrize("failure", ["exception", "cycle"])
async def test_swallowed_child_failure_invalidates_the_whole_traversal(
    database_session_factory, failure
):
    _, record = await saved_plan(database_session_factory)

    @reuse_validated_source
    async def child(session, row):
        if failure == "cycle":
            return await child(session, row)
        raise ValueError("子来源校验失败")

    @reuse_validated_source
    async def root(session, row):
        with suppress(ValueError, TaskFactConflict):
            await child(session, row)
        return source_view(row)

    async with database_session_factory() as session, session.begin():
        row = await session.get(InvestigationCoordinationPlanRow, record.round_id)
        with pytest.raises(TaskFactConflict) as caught:
            await root(session, row)
        assert caught.value.code == "source_validation_changed"
