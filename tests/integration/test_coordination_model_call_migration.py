"""真实 PostgreSQL 验证协调模型调用与计划来源迁移。"""

import asyncio
import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from alembic import command
from opsswarm.investigation.coordination.persistence import CoordinationPlanStore
from tests.integration.test_coordination_plan_store import create_args
from tests.task_facts_helpers import BASE_TIME, TaskClock, claimed_run

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def config() -> Config:
    return Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))


async def insert_root_model_call(
    session,
    claim,
    *,
    operation: str,
    result_version: str | None,
) -> UUID:
    call_id = uuid4()
    model_input = (
        json.dumps({"version": "coordination-model-input-v1"})
        if operation == "coordinate"
        else None
    )
    call_result = (
        json.dumps({"kind": "model", "version": result_version})
        if result_version is not None
        else None
    )
    status = "succeeded" if call_result is not None else "reserved"
    await session.execute(
        text(
            "INSERT INTO investigation_call_reservations "
            "(id,investigation_run_id,investigation_attempt_id,task_id,task_attempt_id,"
            "sequence,invocation_index,idempotency_key,operation,kind,status,error_code,"
            "reserved_at,finished_at,context_manifest,model_input,model_allocation,"
            "model_usage,tool_request,call_result) VALUES "
            "(:id,:run_id,:attempt_id,NULL,NULL,1,0,:key,:operation,'model',:status,NULL,"
            ":reserved_at,:finished_at,NULL,CAST(:model_input AS jsonb),"
            "CAST(:model_allocation AS jsonb),NULL,NULL,CAST(:call_result AS jsonb))"
        ),
        {
            "id": call_id,
            "run_id": claim.run.id,
            "attempt_id": claim.attempt.id,
            "key": f"{claim.attempt.id}:root:{operation}",
            "operation": operation,
            "status": status,
            "reserved_at": BASE_TIME,
            "finished_at": BASE_TIME if status == "succeeded" else None,
            "model_input": model_input,
            "model_allocation": "{}" if model_input is not None else None,
            "call_result": call_result,
        },
    )
    return call_id


async def attach_source_without_immutability_trigger(session, round_id, source_call_id) -> None:
    await session.execute(
        text(
            "ALTER TABLE investigation_coordination_plans "
            "DISABLE TRIGGER trg_coordination_plans_immutable"
        )
    )
    await session.execute(
        text(
            "UPDATE investigation_coordination_plans SET source_call_id=:source_call_id "
            "WHERE round_id=:round_id"
        ),
        {"source_call_id": source_call_id, "round_id": round_id},
    )
    await session.execute(
        text(
            "ALTER TABLE investigation_coordination_plans "
            "ENABLE TRIGGER trg_coordination_plans_immutable"
        )
    )


async def test_upgrade_preserves_existing_calls_and_plans_without_backfill(
    database_session_factory,
):
    _, claim = await claimed_run(database_session_factory)
    plan, _ = await CoordinationPlanStore(database_session_factory, clock=TaskClock()).create_plan(
        claim.run.id, **create_args(claim)
    )
    async with database_session_factory() as session:
        before = await session.scalar(
            text(
                "SELECT to_jsonb(p) - 'source_call_id' "
                "FROM investigation_coordination_plans p WHERE round_id=:round_id"
            ),
            {"round_id": plan.round_id},
        )

    await asyncio.to_thread(command.downgrade, config(), "0041_dynamic_coordination_plans")
    try:
        await asyncio.to_thread(command.upgrade, config(), "head")
        async with database_session_factory() as session:
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT to_jsonb(p) - 'source_call_id' AS original, source_call_id "
                            "FROM investigation_coordination_plans p WHERE round_id=:round_id"
                        ),
                        {"round_id": plan.round_id},
                    )
                )
                .mappings()
                .one()
            )
        assert row["original"] == before
        assert row["source_call_id"] is None
    finally:
        await asyncio.to_thread(command.upgrade, config(), "head")


async def test_coordinate_payload_and_composite_plan_source_are_enforced(
    database_session_factory,
):
    _, first = await claimed_run(database_session_factory)
    _, second = await claimed_run(database_session_factory)
    plan, _ = await CoordinationPlanStore(database_session_factory, clock=TaskClock()).create_plan(
        first.run.id, **create_args(first)
    )

    async with database_session_factory() as session, session.begin():
        first_call = await insert_root_model_call(
            session,
            first,
            operation="coordinate",
            result_version="coordination-model-result-v1",
        )
        second_call = await insert_root_model_call(
            session,
            second,
            operation="coordinate",
            result_version="coordination-model-result-v1",
        )
        await attach_source_without_immutability_trigger(session, plan.round_id, first_call)

    with pytest.raises(IntegrityError):
        async with database_session_factory() as session, session.begin():
            await session.execute(
                text(
                    "ALTER TABLE investigation_coordination_plans "
                    "DISABLE TRIGGER trg_coordination_plans_immutable"
                )
            )
            await session.execute(
                text(
                    "UPDATE investigation_coordination_plans SET source_call_id=:source_call_id "
                    "WHERE round_id=:round_id"
                ),
                {"source_call_id": second_call, "round_id": plan.round_id},
            )

    with pytest.raises(IntegrityError):
        async with database_session_factory() as session, session.begin():
            await session.execute(
                text(
                    "UPDATE investigation_call_reservations SET "
                    'call_result = \'{"kind":"model","version":"wrong-v1"}\'::jsonb '
                    "WHERE id=:call_id"
                ),
                {"call_id": first_call},
            )

    with pytest.raises(IntegrityError):
        async with database_session_factory() as session, session.begin():
            await session.execute(
                text(
                    "UPDATE investigation_call_reservations SET invocation_index=1, "
                    "idempotency_key=:key WHERE id=:call_id"
                ),
                {
                    "call_id": first_call,
                    "key": f"{first.attempt.id}:root:coordinate:1",
                },
            )

    async with database_session_factory() as session:
        source = await session.scalar(
            text(
                "SELECT source_call_id FROM investigation_coordination_plans "
                "WHERE round_id=:round_id"
            ),
            {"round_id": plan.round_id},
        )
        definition = await session.scalar(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname='fk_coordination_plans_source_call'"
            )
        )
    assert source == first_call
    assert "FOREIGN KEY (source_call_id, investigation_run_id)" in definition
    assert "REFERENCES investigation_call_reservations(id, investigation_run_id)" in definition


@pytest.mark.parametrize("fact", ["coordinate", "source"])
async def test_downgrade_refuses_coordination_call_or_plan_source(
    database_session_factory,
    fact,
):
    _, claim = await claimed_run(database_session_factory)
    if fact == "coordinate":
        async with database_session_factory() as session, session.begin():
            await insert_root_model_call(
                session,
                claim,
                operation="coordinate",
                result_version="coordination-model-result-v1",
            )
    else:
        plan, _ = await CoordinationPlanStore(
            database_session_factory, clock=TaskClock()
        ).create_plan(claim.run.id, **create_args(claim))
        async with database_session_factory() as session, session.begin():
            source_call_id = await insert_root_model_call(
                session,
                claim,
                operation="synthesize",
                result_version=None,
            )
            await attach_source_without_immutability_trigger(session, plan.round_id, source_call_id)

    with pytest.raises(DBAPIError, match="不能删除"):
        await asyncio.to_thread(command.downgrade, config(), "0041_dynamic_coordination_plans")
    async with database_session_factory() as session:
        assert (
            await session.scalar(text("SELECT version_num FROM alembic_version"))
            == "0077_candidate_conditions"
        )
