"""真实PG验证现场动态图创建、来源冻结和原生精确窗口守卫。"""

import asyncio
import json
from datetime import timedelta
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from alembic import command
from opsswarm.investigation.models import InvestigationDataSourceMode
from opsswarm.investigation.tasks.evidence import (
    TaskEvidenceSnapshot,
    evidence_hash,
    live_snapshot_id,
)
from opsswarm.investigation.workflow_versions import WorkflowVersion
from tests.live_dynamic_postgres import (
    live_dynamic_postgres_config as live_dynamic_postgres_config,
)
from tests.live_dynamic_postgres import (
    migrated_database_url as migrated_database_url,
)
from tests.loop_helpers import loop_policy
from tests.task_facts_helpers import started_run

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def start_live(factory):
    return await started_run(
        factory,
        workflow_version=WorkflowVersion.V3,
        data_source_mode=InvestigationDataSourceMode.LIVE,
        model_budget=loop_policy(),
    )


async def test_live_dynamic_metadata_and_empty_roundtrip(database_session_factory):
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    await asyncio.to_thread(command.check, config)
    try:
        await asyncio.to_thread(command.downgrade, config, "0096_reflow_approval_decision")
        async with database_session_factory() as session:
            assert (
                await session.scalar(
                    text("SELECT to_regprocedure('protect_live_dynamic_source()')")
                )
                is None
            )
    finally:
        await asyncio.to_thread(command.upgrade, config, "head")
    await asyncio.to_thread(command.check, config)


async def test_live_v3_pins_version_cutoff_and_policy(database_session_factory):
    store, started = await start_live(database_session_factory)
    run = await store.get_run(started.run.id)
    assert run.graph_version == "dynamic-investigation-graph-v16"
    assert run.evidence_policy_version == "task-evidence-live-dynamic-mcp-v1"
    assert run.observation_cutoff_at == run.created_at.replace(microsecond=0)
    assert run.context_policy_version == "summary-context-v1"
    assert run.coordination_policy_version == "dynamic-coordination-v1"


@pytest.mark.parametrize(
    "field,value",
    [
        ("observation_cutoff_at", "observation_cutoff_at - INTERVAL '1 second'"),
        ("graph_version", "'dynamic-investigation-graph-v15'"),
        ("data_source_mode", "'frozen'"),
        ("evidence_policy_version", "'task-evidence-live-mcp-v1'"),
    ],
)
async def test_live_source_cannot_be_rebound(database_session_factory, field, value):
    _, started = await start_live(database_session_factory)
    with pytest.raises(DBAPIError, match="创建后不可修改"):
        async with database_session_factory() as session, session.begin():
            await session.execute(
                text(f"UPDATE investigation_runs SET {field}={value} WHERE id=:id"),
                {"id": started.run.id},
            )


@pytest.mark.parametrize(
    "seconds,service,cutoff_shift,valid",
    [
        (60, "order-service", 0, True),
        (900, "order-service", 0, True),
        (61, "order-service", 0, False),
        (960, "order-service", 0, False),
        (60, "other-service", 0, False),
        (60, "order-service", 1, False),
    ],
)
async def test_native_dynamic_window_scope(
    database_session_factory, seconds, service, cutoff_shift, valid
):
    _, started = await start_live(database_session_factory)
    cutoff = started.run.observation_cutoff_at
    assignment = dict(
        task_key="log",
        role="log",
        service_name=service,
        window_start=(cutoff - timedelta(seconds=seconds)).isoformat(),
        window_end=(cutoff + timedelta(seconds=cutoff_shift)).isoformat(),
        evidence_snapshot_id=live_snapshot_id(started.run.id, cutoff),
    )

    async def verify():
        async with database_session_factory() as session, session.begin():
            await session.execute(
                text(
                    "SELECT assert_live_dynamic_assignment(r, CAST(:body AS JSONB)) "
                    "FROM investigation_runs r WHERE id=:id"
                ),
                {"id": started.run.id, "body": json.dumps(assignment)},
            )

    if valid:
        await verify()
    else:
        with pytest.raises(DBAPIError, match="专业窗口不匹配"):
            await verify()


async def test_new_live_facts_prevent_downgrade(database_session_factory):
    store, started = await start_live(database_session_factory)
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    with pytest.raises(DBAPIError, match="不能降级"):
        await asyncio.to_thread(command.downgrade, config, "0096_reflow_approval_decision")
    assert (await store.get_run(started.run.id)).graph_version == "dynamic-investigation-graph-v16"


async def test_native_canonical_hash_matches_evidence_contract(database_session_factory):
    _, started = await start_live(database_session_factory)
    cutoff = started.run.observation_cutoff_at
    snapshot = TaskEvidenceSnapshot(
        version="task-evidence-live-dynamic-mcp-v1",
        data_source_mode="live",
        evidence_snapshot_id=live_snapshot_id(started.run.id, cutoff),
        role="log",
        service_name="order-service",
        window_end=cutoff,
        window_start=cutoff - timedelta(minutes=1),
        evidence=(),
    )
    async with database_session_factory() as session:
        actual = await session.scalar(
            text(
                "SELECT encode(sha256(convert_to(candidate_canonical_json("
                "CAST(:body AS JSONB)),'UTF8')),'hex')"
            ),
            {"body": snapshot.model_dump_json()},
        )
    assert actual == evidence_hash(snapshot)
