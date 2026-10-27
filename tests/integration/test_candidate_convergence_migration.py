"""真实隔离 PostgreSQL 验证事故收敛原生来源、原子性和历史。"""

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from alembic import command
from tests.api.test_candidate_convergence_api import prepare_settled
from tests.api.test_human_wait_api import wait_context as wait_context
from tests.api.test_release_execution_jobs_api import (
    current_fixture_time as current_fixture_time,
)
from tests.integration import test_candidate_approval_migration as old
from tests.integration.test_candidate_recovery_migration import (
    completed_blocked_execution,
    save_recovery,
)
from tests.integration.test_candidate_settlement_migration import (
    save_settlement,
    settlement_values,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def prepare_uncertain(c):
    job = await completed_blocked_execution(c)
    await save_recovery(c, job)
    values = await settlement_values(c)
    async with c.db() as session, session.begin():
        await save_settlement(session, values)
    return values


async def convergence_values(c):
    async with c.db() as session:
        source = (
            (
                await session.execute(
                    text(
                        "SELECT run.id AS run,run.incident_id AS incident,incident.status,"
                        "incident.version,clock_timestamp() AS now,"
                        "(SELECT id::text FROM alerts WHERE incident_id=incident.id "
                        "ORDER BY ingested_at DESC,id DESC LIMIT 1) AS alert_id "
                        "FROM investigation_runs run "
                        "JOIN incidents incident ON incident.id=run.incident_id "
                        "JOIN investigation_candidate_settlements settlement "
                        "ON settlement.investigation_run_id=run.id"
                    )
                )
            )
            .mappings()
            .one()
        )
        content = await session.scalar(
            text(
                "SELECT candidate_convergence_expected(ROW(:run,:incident,CAST(:content AS jsonb),"
                ":hash,:now)::candidate_incident_convergences)"
            ),
            {
                **source,
                "content": old.encoded(
                    dict(
                        before_status=source["status"],
                        before_version=source["version"],
                        latest_alert_id=source["alert_id"],
                    )
                ),
                "hash": "0" * 64,
            },
        )
        return dict(
            run=source["run"], incident=source["incident"], now=source["now"], content=content
        )


async def insert_convergence(session, values):
    await session.execute(
        text(
            "INSERT INTO candidate_incident_convergences "
            "(investigation_run_id,incident_id,content,content_sha256,created_at) "
            "VALUES (:run,:incident,CAST(:content AS jsonb),:hash,:now)"
        ),
        {
            **values,
            "content": old.encoded(values["content"]),
            "hash": old.digest(old.encoded(values["content"])),
        },
    )


async def save_actual(session, values, *, events=True, marker=True, skip_verifying=False):
    """通过原生 SQL 验证迁移约束，不借用130应用服务补齐缺失事实。"""
    await insert_convergence(session, values)
    body = values["content"]
    for step, action in enumerate(body["actions"], 1):
        if skip_verifying and step == 1:
            continue
        status = "verifying" if step == 1 else body["after_status"]
        await session.execute(
            text(
                "UPDATE incidents SET status=:status,version=:version,updated_at=:now,"
                "resolved_at=:resolved WHERE id=:incident"
            ),
            {
                **values,
                "status": status,
                "version": body["before_version"] + step,
                "resolved": values["now"] if status == "resolved" else None,
            },
        )
        if events:
            await session.execute(
                text(
                    "INSERT INTO incident_events (incident_id,event_type,actor_type,actor_id,"
                    "from_status,to_status,reason,incident_version,payload,occurred_at) VALUES "
                    "(:incident,'incident.status_changed','system',:actor,:previous,:status,"
                    ":reason,:version,CAST(:payload AS jsonb),:now)"
                ),
                {
                    **values,
                    "actor": f"candidate-convergence:{values['run']}",
                    "previous": body["before_status"] if step == 1 else "verifying",
                    "status": status,
                    "reason": body["reason_code"],
                    "version": body["before_version"] + step,
                    "payload": old.encoded(dict(action=action)),
                },
            )
    if marker and body["slot_released"]:
        await session.execute(
            text("UPDATE investigation_runs SET candidate_slot_released_at=:now WHERE id=:run"),
            values,
        )


async def test_0085_metadata_and_empty_roundtrip(database_session_factory):
    await asyncio.to_thread(command.check, old.config())
    try:
        await asyncio.to_thread(command.downgrade, old.config(), "0084_candidate_settlement")
        async with database_session_factory() as session:
            assert (
                await session.scalar(text("SELECT to_regclass('candidate_incident_convergences')"))
                is None
            )
            assert (
                await session.scalar(
                    text("SELECT to_regprocedure('guard_candidate_convergence()')")
                )
                is None
            )
        await asyncio.to_thread(command.upgrade, old.config(), "head")
        await asyncio.to_thread(command.check, old.config())
    finally:
        await asyncio.to_thread(command.upgrade, old.config(), "head")


async def test_0085_orphan_marker_record_and_actor_are_rejected(database_session_factory):
    with pytest.raises(DBAPIError, match="事故收敛缺少"):
        async with database_session_factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO candidate_incident_convergences VALUES "
                    "(:run,:incident,'{}',repeat('0',64),clock_timestamp())"
                ),
                dict(run=uuid4(), incident=uuid4()),
            )
    with pytest.raises(DBAPIError, match="事故收敛事件需要"):
        async with database_session_factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO incident_events (incident_id,event_type,actor_type,actor_id,"
                    "from_status,to_status,reason,incident_version,payload,occurred_at) VALUES "
                    "(:incident,'incident.status_changed','system',:actor,'mitigating','verifying',"
                    "'fresh_recovery_verified',2,'{\"action\":\"complete_mitigation\"}',now())"
                ),
                dict(incident=uuid4(), actor=f"candidate-convergence:{uuid4()}"),
            )


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_0085_uncertain_fact_is_immutable_and_preserves_parent(wait_context):
    c = wait_context
    settlement = await prepare_uncertain(c)
    values = await convergence_values(c)
    assert values["content"]["outcome"] == "held_uncertain"
    assert values["content"]["reason_code"] == "execution_not_applied"
    assert values["content"]["actions"] == []
    assert values["content"]["fresh_until"] is None
    async with c.db() as session, session.begin():
        await insert_convergence(session, values)
    async with c.db() as session:
        await session.execute(
            text(
                "SELECT assert_candidate_convergence_history(item) "
                "FROM candidate_incident_convergences item"
            )
        )
        parent = (
            (
                await session.execute(
                    text(
                        "SELECT status,updated_at,completed_at,candidate_slot_released_at "
                        "FROM investigation_runs WHERE id=:run"
                    ),
                    values,
                )
            )
            .mappings()
            .one()
        )
        assert parent["status"] == "settled"
        assert parent["updated_at"] == parent["completed_at"] == settlement["created_at"]
        assert parent["candidate_slot_released_at"] is None
    for sql in (
        "UPDATE candidate_incident_convergences SET content_sha256=repeat('f',64)",
        "DELETE FROM candidate_incident_convergences",
        "UPDATE investigation_runs SET candidate_slot_released_at=clock_timestamp()",
        "UPDATE investigation_runs SET updated_at=clock_timestamp()",
    ):
        with pytest.raises(DBAPIError, match="不可|槽位"):
            async with c.db() as session, session.begin():
                await session.execute(text(sql))
    with pytest.raises(DBAPIError, match="受控收敛事实"):
        async with c.db() as session, session.begin():
            await session.execute(text("UPDATE incidents SET status='verifying',version=version+1"))
    with pytest.raises(DBAPIError, match="受控收敛事实"):
        async with c.db() as session, session.begin():
            await session.execute(text("UPDATE incidents SET version=version+1"))
    with pytest.raises(DBAPIError, match="既有事故事件"):
        # 仅在隔离事务中模拟正文与摘要同时损坏；回滚恢复事实与触发器。
        async with c.db() as session, session.begin():
            await session.execute(
                text(
                    "ALTER TABLE candidate_incident_convergences "
                    "DISABLE TRIGGER candidate_convergence_guard"
                )
            )
            forged = {
                **values["content"],
                "before_version": values["content"]["before_version"] + 100,
                "after_version": values["content"]["after_version"] + 100,
                "outcome": "held_incident_changed",
                "reason_code": "incident_basis_changed",
            }
            await session.execute(
                text(
                    "UPDATE candidate_incident_convergences SET content=CAST(:content AS jsonb),"
                    "content_sha256=:hash"
                ),
                dict(content=old.encoded(forged), hash=old.digest(old.encoded(forged))),
            )
            await session.execute(
                text(
                    "ALTER TABLE candidate_incident_convergences "
                    "ENABLE TRIGGER candidate_convergence_guard"
                )
            )
            await session.execute(
                text(
                    "SELECT assert_candidate_convergence_history(item) "
                    "FROM candidate_incident_convergences item"
                )
            )
    with pytest.raises(Exception, match="不能删除已保存的事故收敛事实"):
        await asyncio.to_thread(command.downgrade, old.config(), "0084_candidate_settlement")


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
@pytest.mark.parametrize("invalid", ["outcome", "extra", "hash", "boolean", "source"])
async def test_0085_rehashed_forgery_cannot_replace_deterministic_decision(wait_context, invalid):
    c = wait_context
    await prepare_uncertain(c)
    values = await convergence_values(c)
    if invalid == "outcome":
        values["content"]["outcome"] = "recovered"
    elif invalid == "extra":
        values["content"]["extra"] = "injected"
    elif invalid == "hash":
        values["content"]["settlement_content_sha256"] = "f" * 64
    elif invalid == "boolean":
        values["content"]["slot_released"] = 0
    elif invalid == "source":
        values["content"]["investigation_run_id"] = str(uuid4())
    with pytest.raises(DBAPIError, match="事故收敛需要"):
        async with c.db() as session, session.begin():
            await insert_convergence(session, values)


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_0085_pending_settlement_blocks_direct_incident_recovery(wait_context):
    c = wait_context
    await prepare_uncertain(c)
    with pytest.raises(DBAPIError, match="必须先保存受控收敛事实"):
        async with c.db() as session, session.begin():
            await session.execute(text("UPDATE incidents SET status='verifying',version=version+1"))
    with pytest.raises(DBAPIError, match="槽位"):
        async with c.db() as session, session.begin():
            await session.execute(
                text("UPDATE investigation_runs SET candidate_slot_released_at=clock_timestamp()")
            )
    values = await convergence_values(c)
    # held与额外事故写入同事务也不能借已存在的事实绕开资格校验。
    with pytest.raises(DBAPIError, match="受控两跳|受控收敛事实"):
        async with c.db() as session, session.begin():
            await insert_convergence(session, values)
            await session.execute(text("UPDATE incidents SET version=version+1"))


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
@pytest.mark.parametrize("mode", ["healthy", "not_recovered"])
async def test_0085_native_two_hops_close_atomically_and_keep_readable_history(wait_context, mode):
    c = wait_context
    await prepare_settled(c, mode=mode)
    values = await convergence_values(c)
    assert values["content"]["outcome"] == (
        "recovered" if mode == "healthy" else "observed_failure"
    )
    for options in (dict(events=False), dict(skip_verifying=True)):
        with pytest.raises(DBAPIError, match="事件|两跳"):
            async with c.db() as session, session.begin():
                await save_actual(session, values, **options)
    if mode == "healthy":
        with pytest.raises(DBAPIError, match="槽位"):
            async with c.db() as session, session.begin():
                await save_actual(session, values, marker=False)
    async with c.db() as session, session.begin():
        await save_actual(session, values)
    async with c.db() as session:
        await session.execute(
            text(
                "SELECT assert_candidate_convergence_history(item) "
                "FROM candidate_incident_convergences item"
            )
        )
        stored = (
            (
                await session.execute(
                    text("SELECT status,version FROM incidents WHERE id=:incident"), values
                )
            )
            .mappings()
            .one()
        )
        assert stored["status"] == values["content"]["after_status"]
        assert stored["version"] == values["content"]["before_version"] + 2
        event_ids = list(
            await session.scalars(
                text("SELECT id FROM incident_events WHERE actor_id=:actor ORDER BY id"),
                dict(actor=f"candidate-convergence:{values['run']}"),
            )
        )
        assert len(event_ids) == 2
    for sql in (
        "UPDATE incident_events SET reason='forged' WHERE id=:id",
        "DELETE FROM incident_events WHERE id=:id",
    ):
        with pytest.raises(DBAPIError, match="事故收敛事件不可"):
            async with c.db() as session, session.begin():
                await session.execute(text(sql), dict(id=event_ids[0]))
    async with c.db() as session:
        transaction = await session.begin()
        try:
            clone = text(
                "INSERT INTO investigation_runs SELECT (jsonb_populate_record("
                "NULL::investigation_runs,to_jsonb(run)||jsonb_build_object("
                "'id',CAST(:new_id AS text),'status','queued','completed_at',NULL,"
                "'candidate_slot_released_at',NULL,'graph_thread_id',CAST(:new_id AS text)))).* "
                "FROM investigation_runs run WHERE id=:run"
            )
            if mode == "healthy":
                await session.execute(clone, {**values, "new_id": uuid4()})
            else:
                with pytest.raises(DBAPIError, match="uq_investigation_runs_active_incident"):
                    await session.execute(clone, {**values, "new_id": uuid4()})
        finally:
            # 只核验立即唯一索引的物理行为，不提交没有来源的调查运行。
            await transaction.rollback()
