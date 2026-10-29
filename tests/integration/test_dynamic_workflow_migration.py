"""真实 PostgreSQL 验证 v3 启动契约迁移与降级保护。"""

import asyncio
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import text, update
from sqlalchemy.exc import DBAPIError, IntegrityError

from alembic import command
from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.investigation.workflow_versions import WorkflowVersion
from tests.loop_helpers import loop_policy
from tests.task_facts_helpers import BASE_TIME, started_run

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def config() -> Config:
    return Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))


async def test_dynamic_policy_constraint_rejects_partial_v3_snapshot(
    database_session_factory,
) -> None:
    _, started = await started_run(
        database_session_factory,
        workflow_version=WorkflowVersion.V2,
        model_budget=loop_policy(),
        context_policy_version="summary-context-v1",
    )

    with pytest.raises(IntegrityError):
        async with database_session_factory() as session, session.begin():
            await session.execute(
                update(InvestigationRunRow)
                .where(InvestigationRunRow.id == started.run.id)
                .values(workflow_version="v3")
            )


async def test_dynamic_policy_constraint_rejects_graph_version_mutation(
    database_session_factory,
) -> None:
    _, started = await started_run(
        database_session_factory,
        workflow_version=WorkflowVersion.V3,
        model_budget=loop_policy(),
    )

    with pytest.raises(IntegrityError):
        async with database_session_factory() as session, session.begin():
            await session.execute(
                update(InvestigationRunRow)
                .where(InvestigationRunRow.id == started.run.id)
                .values(graph_version="unknown-graph-v1")
            )


async def test_migration_preserves_v2_when_downgraded_and_upgraded(
    database_session_factory,
) -> None:
    _, started = await started_run(
        database_session_factory,
        workflow_version=WorkflowVersion.V2,
        model_budget=loop_policy(),
        context_policy_version="summary-context-v1",
    )

    await asyncio.to_thread(command.downgrade, config(), "0042_coordination_model_call")
    try:
        async with database_session_factory() as session:
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT workflow_version, context_policy_version "
                            "FROM investigation_runs WHERE id=:run_id"
                        ),
                        {"run_id": started.run.id},
                    )
                )
                .mappings()
                .one()
            )
        assert row["workflow_version"] == "v2"
        assert row["context_policy_version"] == "summary-context-v1"
    finally:
        await asyncio.to_thread(command.upgrade, config(), "head")


async def test_synthesis_version_downgrade_refuses_persisted_v3_graph(
    database_session_factory,
) -> None:
    _, started = await started_run(
        database_session_factory,
        workflow_version=WorkflowVersion.V3,
        model_budget=loop_policy(),
    )
    async with database_session_factory() as session, session.begin():
        await session.execute(
            update(InvestigationRunRow)
            .where(InvestigationRunRow.id == started.run.id)
            .values(graph_version="dynamic-investigation-graph-v3")
        )

    try:
        with pytest.raises(DBAPIError, match="不能删除已保存的动态综合图或开放假设"):
            await asyncio.to_thread(command.downgrade, config(), "0044_dynamic_graph_fanout")

        async with database_session_factory() as session:
            assert (
                await session.scalar(text("SELECT version_num FROM alembic_version"))
                == "0092_reflow_routing"
            )
            assert (
                await session.scalar(
                    text("SELECT graph_version FROM investigation_runs WHERE id=:run_id"),
                    {"run_id": started.run.id},
                )
                == "dynamic-investigation-graph-v3"
            )
    finally:
        await asyncio.to_thread(command.upgrade, config(), "head")


async def test_review_version_downgrade_refuses_persisted_v4_graph(
    database_session_factory,
) -> None:
    _, started = await started_run(
        database_session_factory,
        workflow_version=WorkflowVersion.V3,
        model_budget=loop_policy(),
    )
    async with database_session_factory() as session, session.begin():
        await session.execute(
            update(InvestigationRunRow)
            .where(InvestigationRunRow.id == started.run.id)
            .values(graph_version="dynamic-investigation-graph-v4")
        )

    with pytest.raises(DBAPIError, match="不能删除已保存的动态审查图或开放假设审查事实"):
        await asyncio.to_thread(command.downgrade, config(), "0045_dynamic_synthesis")

    async with database_session_factory() as session:
        assert (
            await session.scalar(text("SELECT version_num FROM alembic_version"))
            == "0092_reflow_routing"
        )
        assert (
            await session.scalar(
                text("SELECT graph_version FROM investigation_runs WHERE id=:run_id"),
                {"run_id": started.run.id},
            )
            == "dynamic-investigation-graph-v4"
        )


async def test_routing_version_downgrade_refuses_persisted_v5_graph(
    database_session_factory,
) -> None:
    _, started = await started_run(
        database_session_factory,
        workflow_version=WorkflowVersion.V3,
        model_budget=loop_policy(),
    )
    async with database_session_factory() as session, session.begin():
        await session.execute(
            update(InvestigationRunRow)
            .where(InvestigationRunRow.id == started.run.id)
            .values(graph_version="dynamic-investigation-graph-v5")
        )

    with pytest.raises(DBAPIError, match="不能删除已保存的确定性审查路由图或路由事实"):
        await asyncio.to_thread(command.downgrade, config(), "0046_dynamic_review")

    async with database_session_factory() as session:
        assert (
            await session.scalar(text("SELECT version_num FROM alembic_version"))
            == "0092_reflow_routing"
        )
        assert (
            await session.scalar(
                text("SELECT graph_version FROM investigation_runs WHERE id=:run_id"),
                {"run_id": started.run.id},
            )
            == "dynamic-investigation-graph-v5"
        )


async def test_follow_up_version_downgrade_refuses_persisted_v6_graph(
    database_session_factory,
) -> None:
    _, started = await started_run(
        database_session_factory,
        workflow_version=WorkflowVersion.V3,
        model_budget=loop_policy(),
    )
    async with database_session_factory() as session, session.begin():
        await session.execute(
            update(InvestigationRunRow)
            .where(InvestigationRunRow.id == started.run.id)
            .values(graph_version="dynamic-investigation-graph-v6")
        )

    with pytest.raises(DBAPIError, match="不能删除已保存的补查计划图或补查计划来源"):
        await asyncio.to_thread(command.downgrade, config(), "0047_review_routing")

    async with database_session_factory() as session:
        assert (
            await session.scalar(text("SELECT version_num FROM alembic_version"))
            == "0092_reflow_routing"
        )
        assert (
            await session.scalar(
                text("SELECT graph_version FROM investigation_runs WHERE id=:run_id"),
                {"run_id": started.run.id},
            )
            == "dynamic-investigation-graph-v6"
        )


async def test_second_round_version_downgrade_refuses_persisted_v7_graph(
    database_session_factory,
) -> None:
    _, started = await started_run(
        database_session_factory,
        workflow_version=WorkflowVersion.V3,
        model_budget=loop_policy(),
    )
    async with database_session_factory() as session, session.begin():
        await session.execute(
            update(InvestigationRunRow)
            .where(InvestigationRunRow.id == started.run.id)
            .values(graph_version="dynamic-investigation-graph-v7")
        )

    with pytest.raises(DBAPIError, match="不能删除已保存的第二轮专业调查图"):
        await asyncio.to_thread(command.downgrade, config(), "0048_follow_up_planning")

    async with database_session_factory() as session:
        assert (
            await session.scalar(text("SELECT version_num FROM alembic_version"))
            == "0092_reflow_routing"
        )
        assert (
            await session.scalar(
                text("SELECT graph_version FROM investigation_runs WHERE id=:run_id"),
                {"run_id": started.run.id},
            )
            == "dynamic-investigation-graph-v7"
        )


async def test_second_round_synthesis_downgrade_refuses_persisted_v8_graph(
    database_session_factory,
) -> None:
    _, started = await started_run(
        database_session_factory,
        workflow_version=WorkflowVersion.V3,
        model_budget=loop_policy(),
    )
    async with database_session_factory() as session, session.begin():
        await session.execute(
            update(InvestigationRunRow)
            .where(InvestigationRunRow.id == started.run.id)
            .values(graph_version="dynamic-investigation-graph-v8")
        )

    with pytest.raises(DBAPIError, match="不能删除已保存的第二轮综合图"):
        await asyncio.to_thread(
            command.downgrade,
            config(),
            "0049_second_round_investigation",
        )

    async with database_session_factory() as session:
        assert (
            await session.scalar(text("SELECT version_num FROM alembic_version"))
            == "0092_reflow_routing"
        )
        assert (
            await session.scalar(
                text("SELECT graph_version FROM investigation_runs WHERE id=:run_id"),
                {"run_id": started.run.id},
            )
            == "dynamic-investigation-graph-v8"
        )


async def test_second_round_review_downgrade_refuses_persisted_v9_graph(
    database_session_factory,
) -> None:
    _, started = await started_run(
        database_session_factory,
        workflow_version=WorkflowVersion.V3,
        model_budget=loop_policy(),
    )
    async with database_session_factory() as session, session.begin():
        await session.execute(
            update(InvestigationRunRow)
            .where(InvestigationRunRow.id == started.run.id)
            .values(graph_version="dynamic-investigation-graph-v9")
        )

    with pytest.raises(DBAPIError, match="不能删除已保存的第二轮独立审查图"):
        await asyncio.to_thread(
            command.downgrade,
            config(),
            "0050_second_round_synthesis",
        )

    async with database_session_factory() as session:
        assert (
            await session.scalar(text("SELECT version_num FROM alembic_version"))
            == "0092_reflow_routing"
        )
        assert (
            await session.scalar(
                text("SELECT graph_version FROM investigation_runs WHERE id=:run_id"),
                {"run_id": started.run.id},
            )
            == "dynamic-investigation-graph-v9"
        )


async def test_third_round_review_downgrade_refuses_persisted_v14_graph(
    database_session_factory,
) -> None:
    _, started = await started_run(
        database_session_factory,
        workflow_version=WorkflowVersion.V3,
        model_budget=loop_policy(),
    )
    async with database_session_factory() as session, session.begin():
        await session.execute(
            update(InvestigationRunRow)
            .where(InvestigationRunRow.id == started.run.id)
            .values(graph_version="dynamic-investigation-graph-v14")
        )

    with pytest.raises(DBAPIError, match="不能删除已保存的第三轮独立审查图"):
        await asyncio.to_thread(
            command.downgrade,
            config(),
            "0055_third_round_synthesis",
        )

    async with database_session_factory() as session:
        assert (
            await session.scalar(text("SELECT version_num FROM alembic_version"))
            == "0092_reflow_routing"
        )
        assert (
            await session.scalar(
                text("SELECT graph_version FROM investigation_runs WHERE id=:run_id"),
                {"run_id": started.run.id},
            )
            == "dynamic-investigation-graph-v14"
        )


async def test_third_round_routing_downgrade_refuses_persisted_v15_graph(
    database_session_factory,
) -> None:
    _, started = await started_run(
        database_session_factory,
        workflow_version=WorkflowVersion.V3,
        model_budget=loop_policy(),
    )

    with pytest.raises(DBAPIError, match="不能删除已保存的第三轮最大轮次停止路由图"):
        await asyncio.to_thread(
            command.downgrade,
            config(),
            "0056_third_round_review",
        )

    async with database_session_factory() as session:
        assert (
            await session.scalar(text("SELECT version_num FROM alembic_version"))
            == "0092_reflow_routing"
        )
        assert (
            await session.scalar(
                text("SELECT graph_version FROM investigation_runs WHERE id=:run_id"),
                {"run_id": started.run.id},
            )
            == "dynamic-investigation-graph-v15"
        )


async def test_third_round_synthesis_downgrade_refuses_persisted_v13_graph(
    database_session_factory,
) -> None:
    _, started = await started_run(
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

    with pytest.raises(DBAPIError, match="不能删除已保存的第三轮综合图"):
        await asyncio.to_thread(
            command.downgrade,
            config(),
            "0054_third_round_investigation",
        )

    async with database_session_factory() as session:
        assert (
            await session.scalar(text("SELECT version_num FROM alembic_version"))
            == "0092_reflow_routing"
        )
        assert (
            await session.scalar(
                text("SELECT graph_version FROM investigation_runs WHERE id=:run_id"),
                {"run_id": started.run.id},
            )
            == "dynamic-investigation-graph-v13"
        )


async def test_third_round_investigation_downgrade_refuses_persisted_v12_graph(
    database_session_factory,
) -> None:
    _, started = await started_run(
        database_session_factory,
        workflow_version=WorkflowVersion.V3,
        model_budget=loop_policy(),
    )
    async with database_session_factory() as session, session.begin():
        await session.execute(
            update(InvestigationRunRow)
            .where(InvestigationRunRow.id == started.run.id)
            .values(graph_version="dynamic-investigation-graph-v12")
        )

    with pytest.raises(DBAPIError, match="不能删除已保存的第三轮专业调查图"):
        await asyncio.to_thread(
            command.downgrade,
            config(),
            "0053_third_round_planning",
        )

    async with database_session_factory() as session:
        assert (
            await session.scalar(text("SELECT version_num FROM alembic_version"))
            == "0092_reflow_routing"
        )
        assert (
            await session.scalar(
                text("SELECT graph_version FROM investigation_runs WHERE id=:run_id"),
                {"run_id": started.run.id},
            )
            == "dynamic-investigation-graph-v12"
        )


async def test_third_round_planning_downgrade_refuses_persisted_v11_graph(
    database_session_factory,
) -> None:
    _, started = await started_run(
        database_session_factory,
        workflow_version=WorkflowVersion.V3,
        model_budget=loop_policy(),
    )
    async with database_session_factory() as session, session.begin():
        await session.execute(
            update(InvestigationRunRow)
            .where(InvestigationRunRow.id == started.run.id)
            .values(graph_version="dynamic-investigation-graph-v11")
        )

    with pytest.raises(DBAPIError, match="不能删除已保存的第三轮计划图"):
        await asyncio.to_thread(
            command.downgrade,
            config(),
            "0052_second_round_routing",
        )

    async with database_session_factory() as session:
        assert (
            await session.scalar(text("SELECT version_num FROM alembic_version"))
            == "0092_reflow_routing"
        )
        assert (
            await session.scalar(
                text("SELECT graph_version FROM investigation_runs WHERE id=:run_id"),
                {"run_id": started.run.id},
            )
            == "dynamic-investigation-graph-v11"
        )


async def test_second_round_routing_downgrade_refuses_persisted_v10_graph(
    database_session_factory,
) -> None:
    _, started = await started_run(
        database_session_factory,
        workflow_version=WorkflowVersion.V3,
        model_budget=loop_policy(),
    )
    async with database_session_factory() as session, session.begin():
        await session.execute(
            update(InvestigationRunRow)
            .where(InvestigationRunRow.id == started.run.id)
            .values(graph_version="dynamic-investigation-graph-v10")
        )

    with pytest.raises(DBAPIError, match="不能删除已保存的第二轮确定性路由图"):
        await asyncio.to_thread(
            command.downgrade,
            config(),
            "0051_second_round_review",
        )

    async with database_session_factory() as session:
        assert (
            await session.scalar(text("SELECT version_num FROM alembic_version"))
            == "0092_reflow_routing"
        )


async def test_second_round_review_downgrade_refuses_persisted_second_review_call(
    database_session_factory,
) -> None:
    store, started = await started_run(
        database_session_factory,
        workflow_version=WorkflowVersion.V3,
        model_budget=loop_policy(),
    )
    claim = await store.claim_next(
        worker_id="second-review-migration-test",
        occurred_at=BASE_TIME + timedelta(seconds=2),
        lease_duration=timedelta(seconds=30),
        supported_workflow_versions=(WorkflowVersion.V3,),
    )
    assert claim is not None
    call_id = uuid4()
    async with database_session_factory() as session, session.begin():
        await session.execute(
            update(InvestigationRunRow)
            .where(InvestigationRunRow.id == started.run.id)
            .values(graph_version="dynamic-investigation-graph-v8")
        )
        await session.execute(
            text(
                "INSERT INTO investigation_call_reservations "
                "(id,investigation_run_id,investigation_attempt_id,sequence,invocation_index,"
                "idempotency_key,operation,kind,status,reserved_at) VALUES "
                "(:id,:run_id,:attempt_id,1,1,:key,'review','model','reserved',:reserved_at)"
            ),
            {
                "id": call_id,
                "run_id": started.run.id,
                "attempt_id": claim.attempt.id,
                "key": f"{claim.attempt.id}:root:review:1",
                "reserved_at": BASE_TIME + timedelta(seconds=2),
            },
        )

    with pytest.raises(DBAPIError, match="不能删除已保存的第二轮独立审查调用"):
        await asyncio.to_thread(
            command.downgrade,
            config(),
            "0050_second_round_synthesis",
        )

    async with database_session_factory() as session:
        assert (
            await session.scalar(text("SELECT version_num FROM alembic_version"))
            == "0092_reflow_routing"
        )
        assert (
            await session.scalar(
                text(
                    "SELECT invocation_index FROM investigation_call_reservations WHERE id=:call_id"
                ),
                {"call_id": call_id},
            )
            == 1
        )


async def test_fanout_version_downgrade_refuses_persisted_v2_graph(
    database_session_factory,
) -> None:
    _, started = await started_run(
        database_session_factory,
        workflow_version=WorkflowVersion.V3,
        model_budget=loop_policy(),
    )
    async with database_session_factory() as session, session.begin():
        await session.execute(
            update(InvestigationRunRow)
            .where(InvestigationRunRow.id == started.run.id)
            .values(graph_version="dynamic-investigation-graph-v2")
        )

    try:
        with pytest.raises(DBAPIError, match="不能删除已保存的动态专业分派图版本"):
            await asyncio.to_thread(command.downgrade, config(), "0043_dynamic_workflow_start")

        async with database_session_factory() as session:
            assert (
                await session.scalar(text("SELECT version_num FROM alembic_version"))
                == "0092_reflow_routing"
            )
            assert (
                await session.scalar(
                    text("SELECT graph_version FROM investigation_runs WHERE id=:run_id"),
                    {"run_id": started.run.id},
                )
                == "dynamic-investigation-graph-v2"
            )
    finally:
        await asyncio.to_thread(command.upgrade, config(), "head")


async def test_fanout_version_downgrade_preserves_legacy_v1_graph(
    database_session_factory,
) -> None:
    _, started = await started_run(
        database_session_factory,
        workflow_version=WorkflowVersion.V3,
        model_budget=loop_policy(),
    )
    async with database_session_factory() as session, session.begin():
        await session.execute(
            update(InvestigationRunRow)
            .where(InvestigationRunRow.id == started.run.id)
            .values(graph_version="dynamic-investigation-graph-v1")
        )

    await asyncio.to_thread(command.downgrade, config(), "0043_dynamic_workflow_start")
    try:
        async with database_session_factory() as session:
            assert (
                await session.scalar(
                    text("SELECT graph_version FROM investigation_runs WHERE id=:run_id"),
                    {"run_id": started.run.id},
                )
                == "dynamic-investigation-graph-v1"
            )
    finally:
        await asyncio.to_thread(command.upgrade, config(), "head")
