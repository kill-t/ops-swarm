"""真实隔离PostgreSQL验证候选收尾的来源、原子闭环和不可变历史。"""

import asyncio
from datetime import datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from alembic import command
from tests.api.test_human_wait_api import wait_context as wait_context
from tests.api.test_release_execution_jobs_api import (
    current_fixture_time as current_fixture_time,
)
from tests.integration import test_candidate_approval_migration as old
from tests.integration.test_candidate_recovery_migration import (
    completed_blocked_execution,
    save_recovery,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def settlement_values(c):
    async with c.db() as session:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT r.investigation_run_id,r.id AS recovery_job_id,r.candidate_job_id,"
                        "r.verification_version,r.operation_id,r.execution_result_sha256,"
                        "result.content_sha256 AS recovery_result_sha256,h.id AS handoff_id,"
                        "run.status AS previous_run_status,"
                        "run.updated_at AS previous_run_updated_at,"
                        "request.incident_version,result.content->>'outcome' AS outcome,"
                        "'human_resolution_required' AS disposition,"
                        "result.content->>'reason_code' AS reason_code,"
                        "1+(SELECT coalesce(max(cursor),0) FROM agent_task_events "
                        "WHERE investigation_run_id=run.id) AS event_cursor,"
                        "'candidate-settlement-v1' AS policy_version,"
                        "clock_timestamp() AS created_at "
                        "FROM candidate_recovery_jobs r JOIN candidate_recovery_results result "
                        "ON result.job_id=r.id JOIN candidate_release_execution_jobs original "
                        "ON original.id=r.id JOIN candidate_release_execution_requests request "
                        "ON request.id=original.request_id JOIN investigation_runs run "
                        "ON run.id=r.investigation_run_id JOIN investigation_terminal_handoffs h "
                        "ON h.investigation_run_id=run.id"
                    )
                )
            )
            .mappings()
            .one()
        )
        values = dict(row)
    values["id"] = values["investigation_run_id"]
    return values


def with_digest(values):
    content = {
        key: (
            str(value)
            if isinstance(value, UUID)
            else value.isoformat().replace("+00:00", "Z")
            if isinstance(value, datetime)
            else value
        )
        for key, value in values.items()
        if key != "content_sha256"
    }
    return {**values, "content_sha256": old.digest(old.encoded(content))}


async def save_settlement(session, values, *, event=True, run=True):
    values = with_digest(values)
    await session.execute(
        text(
            "INSERT INTO investigation_candidate_settlements ("
            + ",".join(values)
            + ") VALUES ("
            + ",".join(":" + key for key in values)
            + ")"
        ),
        values,
    )
    if event:
        await session.execute(
            text(
                "INSERT INTO agent_task_events "
                "(investigation_run_id,cursor,event_type,task_id,occurred_at) VALUES "
                "(:investigation_run_id,:event_cursor,'run.candidate_settled',NULL,:created_at)"
            ),
            values,
        )
    if run:
        await session.execute(
            text(
                "UPDATE investigation_runs SET status='settled',updated_at=:created_at,"
                "completed_at=:created_at WHERE id=:investigation_run_id"
            ),
            values,
        )


async def test_0084_metadata_and_empty_roundtrip(database_session_factory):
    await asyncio.to_thread(command.check, old.config())
    try:
        await asyncio.to_thread(command.downgrade, old.config(), "0083_candidate_recovery")
        async with database_session_factory() as session:
            assert (
                await session.scalar(
                    text("SELECT to_regclass('investigation_candidate_settlements')")
                )
                is None
            )
            assert (
                await session.scalar(
                    text("SELECT to_regprocedure('guard_candidate_settlement_run()')")
                )
                is None
            )
        await asyncio.to_thread(command.upgrade, old.config(), "head")
        await asyncio.to_thread(command.check, old.config())
    finally:
        await asyncio.to_thread(command.upgrade, old.config(), "head")


async def test_0084_isolated_state_or_event_cannot_create_settlement(database_session_factory):
    with pytest.raises(DBAPIError, match="原等待运行"):
        async with database_session_factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO investigation_runs "
                    "(id,incident_id,mode,data_source_mode,status,prompt_version,created_at,"
                    "started_at,completed_at,updated_at) VALUES "
                    "(:id,:incident,'single_agent','frozen','settled','v1',now(),now(),now(),now())"
                ),
                dict(id=uuid4(), incident=uuid4()),
            )
    with pytest.raises(DBAPIError, match="同事务事实"):
        async with database_session_factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO agent_task_events "
                    "(investigation_run_id,cursor,event_type,task_id,occurred_at) "
                    "VALUES (:run,1,'run.candidate_settled',NULL,now())"
                ),
                dict(run=uuid4()),
            )


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_0084_record_event_run_close_atomically_and_are_immutable(wait_context):
    c = wait_context
    job = await completed_blocked_execution(c)
    await save_recovery(c, job)
    values = await settlement_values(c)
    with pytest.raises(DBAPIError, match="精确绑定"):
        async with c.db() as session, session.begin():
            await session.execute(
                text(
                    "UPDATE investigation_runs SET status='settled',"
                    "completed_at=clock_timestamp() WHERE id=:id"
                ),
                dict(id=values["investigation_run_id"]),
            )
    for event, run in ((False, False), (True, False)):
        with pytest.raises(DBAPIError, match="闭环|父终态|外键|foreign key"):
            async with c.db() as session, session.begin():
                await save_settlement(session, values, event=event, run=run)
        async with c.db() as session:
            assert (
                await session.scalar(
                    text("SELECT count(*) FROM investigation_candidate_settlements")
                )
                == 0
            )
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM agent_task_events "
                        "WHERE event_type='run.candidate_settled'"
                    )
                )
                == 0
            )
            assert (
                await session.scalar(
                    text("SELECT status FROM investigation_runs WHERE id=:id"),
                    dict(id=values["investigation_run_id"]),
                )
                == "await_candidate"
            )
    async with c.db() as session, session.begin():
        await save_settlement(session, values)
    async with c.db() as session:
        await session.execute(
            text("SELECT assert_recovery_history(job) FROM candidate_recovery_jobs job")
        )
        await session.execute(
            text(
                "SELECT assert_candidate_settlement_source(item) "
                "FROM investigation_candidate_settlements item"
            )
        )
        await session.execute(
            text(
                "SELECT assert_candidate_settlement_leaf(item) "
                "FROM investigation_candidate_settlements item"
            )
        )
        active_index = await session.scalar(
            text(
                "SELECT indexdef FROM pg_indexes WHERE indexname="
                "'uq_investigation_runs_active_incident'"
            )
        )
        assert "settled" in active_index
    with pytest.raises(DBAPIError, match="uq_investigation_runs_active_incident"):
        async with c.db() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO investigation_runs SELECT (jsonb_populate_record("
                    "NULL::investigation_runs,to_jsonb(run)||jsonb_build_object("
                    "'id',CAST(:new_id AS text),'status','queued','completed_at',NULL,"
                    "'graph_thread_id',CAST(:new_id AS text)))).* "
                    "FROM investigation_runs run WHERE id=:id"
                ),
                dict(new_id=uuid4(), id=values["investigation_run_id"]),
            )
    for statement in (
        "UPDATE investigation_candidate_settlements SET reason_code='tampered'",
        "DELETE FROM investigation_candidate_settlements",
        "UPDATE investigation_runs SET status='await_candidate',completed_at=NULL",
        "UPDATE investigation_runs SET updated_at=clock_timestamp()",
        "UPDATE agent_task_events SET occurred_at=clock_timestamp() "
        "WHERE event_type='run.candidate_settled'",
        "DELETE FROM agent_task_events WHERE event_type='run.candidate_settled'",
    ):
        with pytest.raises(DBAPIError, match="不可"):
            async with c.db() as session, session.begin():
                await session.execute(text(statement))
    with pytest.raises(Exception, match="不能删除已保存的候选收尾事实"):
        await asyncio.to_thread(command.downgrade, old.config(), "0083_candidate_recovery")


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
@pytest.mark.parametrize(
    "invalid",
    [
        "hash",
        "outcome",
        "previous",
        "incident",
        "current_incident",
        "materials",
        "id",
        "in_flight_candidate",
    ],
)
async def test_0084_rejects_rehashed_inconsistent_sources_and_stale_currentness(
    wait_context, invalid
):
    c = wait_context
    job = await completed_blocked_execution(c)
    await save_recovery(c, job)
    values = await settlement_values(c)
    if invalid == "id":
        values["id"] = uuid4()
    elif invalid == "hash":
        values["recovery_result_sha256"] = "a" * 64
    elif invalid == "outcome":
        values["outcome"] = "recovered"
    elif invalid == "previous":
        values["previous_run_status"] = "await_human"
    elif invalid == "incident":
        values["incident_version"] += 1
    expected_error = "需要当前等待" if invalid == "in_flight_candidate" else "收尾|恢复验证"
    with pytest.raises(DBAPIError, match=expected_error):
        async with c.db() as session, session.begin():
            if invalid == "in_flight_candidate":
                # 调度对抗夹具只新增另一份queued行，不破坏正在验证的原候选来源。
                await session.execute(text("SET LOCAL session_replication_role='replica'"))
                await session.execute(
                    text(
                        "INSERT INTO investigation_candidate_evaluation_jobs SELECT "
                        "(jsonb_populate_record(NULL::investigation_candidate_evaluation_jobs, "
                        "to_jsonb(original)||CAST(:patch AS jsonb))).* "
                        "FROM investigation_candidate_evaluation_jobs original WHERE id=:source"
                    ),
                    {
                        "source": values["candidate_job_id"],
                        "patch": old.encoded(
                            {
                                "id": str(uuid4()),
                                "source_id": str(uuid4()),
                                "authorization_id": str(uuid4()),
                                "idempotency_key": "pending-candidate-129",
                                "status": "queued",
                                "attempt_count": 0,
                                "completed_at": None,
                            }
                        ),
                    },
                )
                await session.execute(text("SET LOCAL session_replication_role='origin'"))
            if invalid == "current_incident":
                await session.execute(
                    text(
                        "UPDATE incidents SET version=version+1 WHERE id="
                        "(SELECT incident_id FROM investigation_runs WHERE id=:id)"
                    ),
                    dict(id=values["investigation_run_id"]),
                )
            if invalid == "materials":
                # 只在隔离容器事务中模拟磁盘历史损坏，失败后连同篡改一起回滚。
                await session.execute(
                    text(
                        "ALTER TABLE candidate_recovery_results "
                        "DISABLE TRIGGER recovery_result_guard"
                    )
                )
                await session.execute(
                    text(
                        "UPDATE candidate_recovery_results "
                        'SET content=content || \'{"reason_code":"wrong"}\''
                    )
                )
                await session.execute(
                    text(
                        "ALTER TABLE candidate_recovery_results "
                        "ENABLE TRIGGER recovery_result_guard"
                    )
                )
            await save_settlement(session, values)
    async with c.db() as session:
        assert (
            await session.scalar(text("SELECT count(*) FROM investigation_candidate_settlements"))
            == 0
        )
