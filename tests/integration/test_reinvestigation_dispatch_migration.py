"""真实PostgreSQL独立验证回流编译、完整来源与整轮原子分派。"""

import asyncio
import copy
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from alembic import command
from opsswarm.approval.conditions.queries import raw_content_hash
from opsswarm.investigation.budget.contracts import CallBudgetSnapshot
from opsswarm.investigation.reinvestigation_coordination.contracts import ReflowResult, ReflowSource
from opsswarm.investigation.reinvestigation_dispatch.policy import compile_dispatch, task_id
from tests.api import test_reinvestigation_coordination_api as api
from tests.api.test_reinvestigation_coordination_api import (
    current_fixture_time as current_fixture_time,
)
from tests.api.test_reinvestigation_coordination_api import (
    initial_reflow_capacity as initial_reflow_capacity,
)
from tests.api.test_reinvestigation_coordination_api import wait_context as wait_context
from tests.integration.test_candidate_approval_migration import config, encoded
from tests.integration.test_reinvestigation_coordination_migration import insert

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def prepared(c, *, reuse=False):
    await api.prepare(c)
    model = api.FakeModel()
    if reuse:
        original = c.reflow_store.complete

        async def crash(*args):
            raise RuntimeError("模拟132成功调用后结果事务失败")

        c.reflow_store.complete = crash
        assert await api.worker(c, model).run_once()
        c.reflow_store.complete = original
        await asyncio.sleep(2)
    assert await api.worker(c, model).run_once()
    assert len(model.calls) == 1
    response = await api.get(c)
    assert response.status_code == 200 and response.json()["status"] == "succeeded", response.text
    async with c.db() as session:
        now = c.clock()
        run_id = c.started.run.id
        job = await session.scalar(
            text("SELECT to_jsonb(j) FROM investigation_reflow_jobs j WHERE id=:id"), {"id": run_id}
        )
        result = await session.scalar(
            text("SELECT to_jsonb(r) FROM investigation_reflow_results r WHERE job_id=:id"),
            {"id": run_id},
        )
        budget = await session.scalar(
            text("SELECT reflow_budget_snapshot(:id,:now)"), {"id": run_id, "now": now}
        )
        service = await session.scalar(
            text("SELECT service_name FROM incidents WHERE id=:id"),
            {"id": c.started.run.incident_id},
        )
        native = await session.scalar(
            text("SELECT reflow_dispatch_content(:id,:now)"), {"id": run_id, "now": now}
        )
        content = compile_dispatch(
            ReflowSource.model_validate(job["source"]),
            ReflowResult.model_validate(result["content"]),
            result_sha256=result["content_sha256"],
            service_name=service,
            budget=CallBudgetSnapshot.model_validate(budget),
            now=now,
            basis_event_cursor=native["basis_event_cursor"],
        ).model_dump(mode="json")
        assert native == content
    dispatch = dict(
        investigation_run_id=str(run_id),
        source_result_sha256=result["content_sha256"],
        status=content["status"],
        content=content,
        content_sha256=raw_content_hash(content),
        created_at=content["created_at"],
    )
    tasks = [
        dict(
            id=str(task_id(run_id, task["task_key"])),
            investigation_run_id=str(run_id),
            round_id=content["round_id"],
            task_key=task["task_key"],
            assignment=task,
            assignment_sha256=raw_content_hash(task),
            status="pending",
            created_at=content["created_at"],
        )
        for task in content["assignment"]["tasks"]
    ]
    return dispatch, tasks


async def publish(session, dispatch, tasks):
    await insert(session, "investigation_reflow_dispatches", dispatch)
    for task in tasks:
        await insert(session, "investigation_reflow_task_queue", task)


async def test_0088_metadata_and_empty_roundtrip(database_session_factory):
    await asyncio.to_thread(command.check, config())
    try:
        await asyncio.to_thread(command.downgrade, config(), "0087_reflow_coordination")
        async with database_session_factory() as session:
            assert (
                await session.scalar(text("SELECT to_regclass('investigation_reflow_dispatches')"))
                is None
            )
    finally:
        await asyncio.to_thread(command.upgrade, config(), "head")
    await asyncio.to_thread(command.check, config())


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_0088_complete_native_compiler_preserves_history_and_immutable_tasks(wait_context):
    c = wait_context
    dispatch, tasks = await prepared(c)
    before = await api.root_facts(c)
    calls = await api.calls(c)
    async with c.db() as session, session.begin():
        await publish(session, dispatch, tasks)
    assert await api.root_facts(c) == before
    assert await api.calls(c) == calls
    response = await api.get(c)
    assert response.status_code == 200 and response.json()["status"] == "succeeded"
    for table in ("investigation_reflow_dispatches", "investigation_reflow_task_queue"):
        for sql in (f"UPDATE {table} SET created_at=created_at", f"DELETE FROM {table}"):
            with pytest.raises(DBAPIError):
                async with c.db() as session, session.begin():
                    await session.execute(text(sql))
    with pytest.raises(DBAPIError):
        await asyncio.to_thread(command.downgrade, config(), "0087_reflow_coordination")
    await asyncio.to_thread(command.check, config())


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_0088_rehashed_forged_content_and_task_bindings_rejected(wait_context):
    c = wait_context
    dispatch, tasks = await prepared(c)
    for mutation in ("source", "budget", "floor", "service", "snapshot", "reason", "round", "goal"):
        forged = copy.deepcopy(dispatch)
        content = forged["content"]
        if mutation == "source":
            content["source_result_sha256"] = "a" * 64
            forged["source_result_sha256"] = "a" * 64
        elif mutation == "budget":
            content["budget"]["calls"] = []
        elif mutation == "floor":
            content["observation_floor_at"] = content["created_at"]
        elif mutation == "service":
            content["service_name"] = "other-service"
        elif mutation == "snapshot":
            content["assignment"]["tasks"][0]["evidence_snapshot_id"] = "old"
        elif mutation == "reason":
            content.update(
                status="blocked",
                reason_code="reflow_source_changed",
                round_id=None,
                assignment=None,
            )
            forged["status"] = "blocked"
        elif mutation == "round":
            content["assignment"]["round_number"] = 1
        else:
            content["assignment"]["tasks"][0]["goal"] = "伪造调查目标"
        forged["content_sha256"] = raw_content_hash(content)
        with pytest.raises(DBAPIError):
            async with c.db() as session, session.begin():
                await publish(session, forged, tasks)
    for field, value in (
        ("id", str(uuid4())),
        ("investigation_run_id", str(uuid4())),
        ("round_id", str(uuid4())),
        ("task_key", "other_key"),
        ("status", "succeeded"),
        ("created_at", "2020-01-01T00:00:00Z"),
    ):
        forged_tasks = copy.deepcopy(tasks)
        forged_tasks[0][field] = value
        with pytest.raises(DBAPIError):
            async with c.db() as session, session.begin():
                await publish(session, dispatch, forged_tasks)
    forged_tasks = copy.deepcopy(tasks)
    forged_tasks[0]["assignment"]["service_name"] = "other-service"
    forged_tasks[0]["assignment_sha256"] = raw_content_hash(forged_tasks[0]["assignment"])
    with pytest.raises(DBAPIError):
        async with c.db() as session, session.begin():
            await publish(session, dispatch, forged_tasks)


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_0088_missing_extra_cross_transaction_and_commit_staleness_rejected(wait_context):
    c = wait_context
    dispatch, tasks = await prepared(c)
    with pytest.raises(DBAPIError, match="完整任务"):
        async with c.db() as session, session.begin():
            await publish(session, dispatch, [])
    # 前一事务已整体回滚，不能在后续事务补齐缺失的分派。
    with pytest.raises(DBAPIError):
        async with c.db() as session, session.begin():
            await insert(session, "investigation_reflow_task_queue", tasks[0])
    extra = {**tasks[0], "task_key": "unrequested", "id": str(uuid4())}
    with pytest.raises(DBAPIError):
        async with c.db() as session, session.begin():
            await publish(session, dispatch, [*tasks, extra])
    with pytest.raises(DBAPIError, match="提交前"):
        async with c.db() as session, session.begin():
            await publish(session, dispatch, tasks)
            await session.execute(
                text("UPDATE incidents SET version=version+1 WHERE id=:id"),
                {"id": c.started.run.incident_id},
            )
    async with c.db() as session:
        assert (
            await session.scalar(text("SELECT count(*) FROM investigation_reflow_dispatches")) == 0
        )


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_0088_stale_source_is_blocked_without_tasks(wait_context):
    c = wait_context
    await prepared(c)
    await api.admission.attach_old_alert(c)
    async with c.db() as session:
        now = c.clock()
        content = await session.scalar(
            text("SELECT reflow_dispatch_content(:id,:now)"), {"id": c.started.run.id, "now": now}
        )
    assert content["status"] == "blocked" and content["reason_code"] == "reflow_source_changed"
    assert content["round_id"] is None and content["assignment"] is None
    dispatch = dict(
        investigation_run_id=str(c.started.run.id),
        content=content,
        content_sha256=raw_content_hash(content),
        status="blocked",
        source_result_sha256=content["source_result_sha256"],
        created_at=now.isoformat(),
    )
    async with c.db() as session, session.begin():
        await publish(session, dispatch, [])
    with pytest.raises(DBAPIError):
        async with c.db() as session, session.begin():
            await insert(
                session,
                "investigation_reflow_task_queue",
                dict(
                    id=str(uuid4()),
                    investigation_run_id=str(c.started.run.id),
                    round_id=str(uuid4()),
                    task_key="forged",
                    assignment={},
                    assignment_sha256=raw_content_hash({}),
                    status="pending",
                    created_at=now.isoformat(),
                ),
            )


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_0088_independent_source_rebuild_rejects_corrupted_success_chain(wait_context):
    c = wait_context
    dispatch, tasks = await prepared(c, reuse=True)
    mutations = (
        "UPDATE incidents SET version=version+1",
        "UPDATE investigation_reflow_jobs SET source=jsonb_set(source,'{existing_task_count}','0')",
        "UPDATE investigation_reflow_jobs SET packet=jsonb_set(packet,'{manifest,materials}','[]')",
        "UPDATE investigation_reflow_results SET content=jsonb_set(content,'{source_sha256}',"
        "to_jsonb(repeat('a',64)))",
        "UPDATE investigation_reflow_results SET investigation_run_id=gen_random_uuid()",
        "UPDATE investigation_reflow_attempts SET status='failed',error_code='corrupted'",
        "UPDATE investigation_call_reservations SET model_input='{}' "
        "WHERE operation='coordinate_reinvestigation'",
        "UPDATE investigation_call_reservations SET reinvestigation_attempt_id=gen_random_uuid() "
        "WHERE operation='coordinate_reinvestigation'",
        "UPDATE investigation_call_reservations SET call_result=jsonb_set(call_result,"
        "'{proposal,goal}',to_jsonb('伪造来源建议'::text)) "
        "WHERE operation='coordinate_reinvestigation'",
    )
    for mutation in mutations:
        with pytest.raises(DBAPIError):
            async with c.db() as session, session.begin():
                # 仅在当前隔离事务模拟旧材料损坏；恢复所有守卫后独立检查133来源。
                await session.execute(text("SET LOCAL session_replication_role='replica'"))
                await session.execute(text(mutation))
                await session.execute(text("SET LOCAL session_replication_role='origin'"))
                await publish(session, dispatch, tasks)
    # 每个损坏事务均回滚；整套分派也允许在同一SAVEPOINT原子创建。
    async with c.db() as session, session.begin(), session.begin_nested():
        await publish(session, dispatch, tasks)


async def test_0088_stable_ids_and_capacity_order(database_session_factory):
    run_id = uuid4()
    async with database_session_factory() as session:
        native = await session.scalar(
            text("SELECT reflow_dispatch_id(:id,:name)"),
            {"id": run_id, "name": "reflow-task-v1:custom_key"},
        )
        assert native == task_id(run_id, "custom_key")
        now = await session.scalar(text("SELECT clock_timestamp()"))
        budget = dict(deadline=(now - timedelta(seconds=1)).isoformat(), calls=[])
        assert (
            await session.scalar(
                text("SELECT reflow_dispatch_capacity(CAST(:budget AS jsonb),2,:now)"),
                {"budget": encoded(budget), "now": now},
            )
            == "run_deadline_exceeded"
        )
