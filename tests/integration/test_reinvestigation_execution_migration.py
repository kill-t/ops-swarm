"""真实PostgreSQL核对独立专业执行的迁移、冻结窗口和原生归属。"""

import asyncio
import copy
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from alembic import command
from opsswarm.approval.conditions.queries import raw_content_hash
from opsswarm.investigation.reinvestigation_execution.window import effective_window
from opsswarm.investigation.tasks.contracts import TaskAssignment
from tests.api.test_reinvestigation_coordination_api import (
    current_fixture_time as current_fixture_time,
)
from tests.api.test_reinvestigation_coordination_api import (
    initial_reflow_capacity as initial_reflow_capacity,
)
from tests.api.test_reinvestigation_coordination_api import wait_context as wait_context
from tests.integration.test_candidate_approval_migration import config, encoded
from tests.integration.test_reinvestigation_coordination_migration import insert
from tests.integration.test_reinvestigation_dispatch_migration import prepared, publish

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_0089_metadata_and_empty_roundtrip(database_session_factory):
    await asyncio.to_thread(command.check, config())
    try:
        await asyncio.to_thread(command.downgrade, config(), "0088_reflow_dispatch")
        async with database_session_factory() as session:
            assert (
                await session.scalar(text("SELECT to_regclass('investigation_reflow_task_work')"))
                is None
            )
    finally:
        await asyncio.to_thread(command.upgrade, config(), "head")
    await asyncio.to_thread(command.check, config())


async def test_0089_native_window_matches_python(database_session_factory):
    async with database_session_factory() as session:
        for role in ("log", "metric", "change"):
            assignment = TaskAssignment(
                task_key="new_task",
                role=role,
                goal="读取新证据",
                service_name="order-service",
                window_start=datetime(2026, 1, 1, 0, 0, 0, 100, tzinfo=UTC),
                window_end=datetime(2026, 1, 1, 0, 2, 0, 400, tzinfo=UTC),
                max_attempts=3,
                evidence_snapshot_id=None,
            )
            native = await session.scalar(
                text("SELECT reflow_execution_window(CAST(:assignment AS jsonb))"),
                {"assignment": encoded(assignment.model_dump(mode="json"))},
            )
            assert native == effective_window(assignment).model_dump(mode="json")


async def execution_prepared(c):
    dispatch, tasks = await prepared(c)
    floor_at = datetime.fromisoformat(dispatch["content"]["observation_floor_at"])
    remaining = 65 - (c.clock() - floor_at).total_seconds()
    while remaining > 0:
        await asyncio.sleep(min(remaining, 1))
        remaining = 65 - (c.clock() - floor_at).total_seconds()
    async with c.db() as session:
        content = await session.scalar(
            text("SELECT reflow_dispatch_content(:id,:now)"),
            {"id": c.started.run.id, "now": c.clock()},
        )
    dispatch.update(
        content=content, content_sha256=raw_content_hash(content), created_at=content["created_at"]
    )
    for task, assignment in zip(tasks, content["assignment"]["tasks"], strict=True):
        task.update(
            assignment=assignment,
            assignment_sha256=raw_content_hash(assignment),
            created_at=content["created_at"],
        )
    async with c.db() as session, session.begin():
        await publish(session, dispatch, tasks)
    now = c.clock()
    works = []
    for task in tasks:
        window = effective_window(TaskAssignment.model_validate(task["assignment"]))
        works.append(
            dict(
                task_id=task["id"],
                investigation_run_id=task["investigation_run_id"],
                dispatch_sha256=dispatch["content_sha256"],
                assignment_sha256=task["assignment_sha256"],
                policy_version="reinvestigation-task-v1",
                evidence_policy_version="reflow-live-mcp-v1",
                effective_window=window.model_dump(mode="json"),
                status="queued",
                attempt_count=0,
                max_attempts=task["assignment"]["max_attempts"],
                available_at=now.isoformat(),
                created_at=now.isoformat(),
                updated_at=now.isoformat(),
                completed_at=None,
                locked_by=None,
                lease_token=None,
                locked_until=None,
                last_error=None,
            )
        )
    return dispatch, tasks, works


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_0089_history_source_and_work_frozen_hashes(wait_context):
    c = wait_context
    dispatch, tasks, works = await execution_prepared(c)
    async with c.db() as session:
        assert (
            await session.scalar(
                text("SELECT reflow_execution_dispatch_content(:id,:now)"),
                {"id": c.started.run.id, "now": dispatch["created_at"]},
            )
            == dispatch["content"]
        )
    for field, value in (
        ("dispatch_sha256", "a" * 64),
        ("assignment_sha256", "a" * 64),
        ("max_attempts", 1),
        ("effective_window", None),
    ):
        forged = copy.deepcopy(works[0])
        forged[field] = value
        if field == "max_attempts" and value == works[0][field]:
            forged[field] = 2
        with pytest.raises(DBAPIError):
            async with c.db() as session, session.begin():
                await insert(session, "investigation_reflow_task_work", forged)
    async with c.db() as session, session.begin(), session.begin_nested():
        for work in works:
            await insert(session, "investigation_reflow_task_work", work)
    with pytest.raises(DBAPIError):
        await asyncio.to_thread(command.downgrade, config(), "0088_reflow_dispatch")
    for sql in (
        "UPDATE investigation_reflow_task_work SET dispatch_sha256=repeat('b',64)",
        "DELETE FROM investigation_reflow_task_work",
    ):
        with pytest.raises(DBAPIError):
            async with c.db() as session, session.begin():
                await session.execute(text(sql))

    with pytest.raises(DBAPIError):
        async with c.db() as session, session.begin():
            await session.execute(text("SET LOCAL session_replication_role='replica'"))
            await session.execute(text("UPDATE incidents SET version=version+1"))
            await session.execute(text("SET LOCAL session_replication_role='origin'"))
            await session.execute(
                text("SELECT assert_reflow_execution_source(:id)"), {"id": tasks[0]["id"]}
            )

    from tests.api import test_reinvestigation_coordination_api as api

    await api.admission.attach_old_alert(c)
    async with c.db() as session:
        await session.execute(
            text("SELECT assert_reflow_execution_source(:id)"), {"id": tasks[0]["id"]}
        )
        assert (
            await session.scalar(
                text("SELECT reflow_basis_blocker(:id,clock_timestamp())"), {"id": c.started.run.id}
            )
            == "reflow_source_changed"
        )
