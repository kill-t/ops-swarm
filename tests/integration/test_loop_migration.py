"""证据读取循环迁移保留旧账本，并拒绝不完整交互与审计降级。"""

import asyncio
import json
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import text, update
from sqlalchemy.exc import DBAPIError, IntegrityError

from alembic import command
from opsswarm.investigation.budget.contracts import CallOperation
from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.investigation.workflow_versions import WorkflowVersion
from tests.integration.test_call_budget_store import prepare
from tests.integration.test_repeated_calls import reserve_slot
from tests.model_budget_helpers import model_input, model_policy
from tests.task_facts_helpers import started_run

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def config() -> Config:
    return Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))


def function_calling_policy():
    original = model_policy()
    return original.model_copy(
        update={
            "profile": original.profile.model_copy(
                update={"structured_output_method": "function_calling"}
            )
        }
    )


async def test_upgrade_preserves_old_calls_and_leaves_new_payload_empty(
    database_session_factory,
):
    _, claim, _, _, attempts, store = await prepare(database_session_factory)
    grant = await reserve_slot(store, claim, attempts["log"], 0)
    async with database_session_factory() as session:
        before = await session.scalar(
            text(
                "SELECT to_jsonb(c) - 'tool_request' - 'call_result' "
                "FROM investigation_call_reservations c WHERE id = :call_id"
            ),
            {"call_id": grant.record.id},
        )

    await asyncio.to_thread(command.downgrade, config(), "0037_call_invocation_index")
    await asyncio.to_thread(command.upgrade, config(), "head")

    async with database_session_factory() as session:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT to_jsonb(c) - 'tool_request' - 'call_result' AS original, "
                        "tool_request, call_result FROM investigation_call_reservations c "
                        "WHERE id = :call_id"
                    ),
                    {"call_id": grant.record.id},
                )
            )
            .mappings()
            .one()
        )
    assert row["original"] == before
    assert row["tool_request"] is None and row["call_result"] is None


async def test_database_requires_function_calling_for_loop_policy(database_session_factory):
    _, incompatible = await started_run(
        database_session_factory,
        workflow_version=WorkflowVersion.V2,
        model_budget=model_policy(),
    )
    with pytest.raises(IntegrityError):
        async with database_session_factory() as session, session.begin():
            await session.execute(
                update(InvestigationRunRow)
                .where(InvestigationRunRow.id == incompatible.run.id)
                .values(context_policy_version="evidence-read-v1")
            )

    _, compatible = await started_run(
        database_session_factory,
        workflow_version=WorkflowVersion.V2,
        model_budget=function_calling_policy(),
    )
    async with database_session_factory() as session, session.begin():
        await session.execute(
            update(InvestigationRunRow)
            .where(InvestigationRunRow.id == compatible.run.id)
            .values(context_policy_version="evidence-read-v1")
        )


async def test_loop_payload_constraint_rejects_old_tools_and_incomplete_results(
    database_session_factory,
):
    policy = function_calling_policy()
    _, claim, _, _, attempts, store = await prepare(
        database_session_factory,
        model_budget=policy,
    )
    task_attempt = attempts["log"]
    tool_call = await reserve_slot(store, claim, task_attempt, 0)
    tool_request = json.dumps(
        {
            "source_call_id": str(tool_call.record.id),
            "tool_call_id": "read-1",
            "arguments": {"evidence_id": "log-1", "snapshot_sha256": "a" * 64},
        }
    )

    with pytest.raises(IntegrityError):
        async with database_session_factory() as session, session.begin():
            await session.execute(
                text(
                    "UPDATE investigation_call_reservations "
                    "SET tool_request = CAST(:request AS JSONB) WHERE id = :call_id"
                ),
                {"request": tool_request, "call_id": tool_call.record.id},
            )

    oversized_request = json.dumps({"padding": "x" * 8193})
    with pytest.raises(IntegrityError):
        async with database_session_factory() as session, session.begin():
            await session.execute(
                text(
                    "UPDATE investigation_call_reservations SET "
                    "operation = 'read_log_evidence', "
                    "idempotency_key = investigation_attempt_id::text || ':' || "
                    "task_attempt_id::text || ':' || 'read_log_evidence', "
                    "tool_request = CAST(:request AS JSONB) WHERE id = :call_id"
                ),
                {"request": oversized_request, "call_id": tool_call.record.id},
            )

    async with database_session_factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE investigation_call_reservations SET "
                "operation = 'read_log_evidence', "
                "idempotency_key = investigation_attempt_id::text || ':' || "
                "task_attempt_id::text || ':' || 'read_log_evidence', "
                "tool_request = CAST(:request AS JSONB) WHERE id = :call_id"
            ),
            {"request": tool_request, "call_id": tool_call.record.id},
        )

    with pytest.raises(IntegrityError):
        async with database_session_factory() as session, session.begin():
            await session.execute(
                text(
                    "UPDATE investigation_call_reservations SET "
                    'call_result = \'{"kind": "tool"}\'::jsonb WHERE id = :call_id'
                ),
                {"call_id": tool_call.record.id},
            )

    async with database_session_factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE investigation_call_reservations SET status = 'succeeded', "
                'finished_at = reserved_at, call_result = \'{"kind": "tool"}\'::jsonb '
                "WHERE id = :call_id"
            ),
            {"call_id": tool_call.record.id},
        )

    for invalid_result in (
        json.dumps({"kind": "model"}),
        json.dumps({"kind": "tool", "padding": "x" * 65537}),
    ):
        with pytest.raises(IntegrityError):
            async with database_session_factory() as session, session.begin():
                await session.execute(
                    text(
                        "UPDATE investigation_call_reservations "
                        "SET call_result = CAST(:result AS JSONB) WHERE id = :call_id"
                    ),
                    {"result": invalid_result, "call_id": tool_call.record.id},
                )

    model_call = await reserve_slot(
        store,
        claim,
        task_attempt,
        1,
        operation=CallOperation.ANALYZE_LOG,
        model_input=model_input(method="function_calling"),
        prompt_version="evidence-read-v1",
    )
    with pytest.raises(IntegrityError):
        async with database_session_factory() as session, session.begin():
            await session.execute(
                text(
                    "UPDATE investigation_call_reservations SET status = 'succeeded', "
                    'finished_at = reserved_at, call_result = \'{"kind": "model"}\'::jsonb '
                    "WHERE id = :call_id"
                ),
                {"call_id": model_call.record.id},
            )

    async with database_session_factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE investigation_call_reservations SET status = 'succeeded', "
                "finished_at = reserved_at, "
                "model_input = jsonb_set(model_input, '{version}', "
                "'\"evidence-loop-input-v1\"'::jsonb), "
                'call_result = \'{"kind": "model"}\'::jsonb WHERE id = :call_id'
            ),
            {"call_id": model_call.record.id},
        )


@pytest.mark.parametrize("audit", ["policy", "model_input", "interaction"])
async def test_downgrade_refuses_any_loop_audit(database_session_factory, audit):
    policy = function_calling_policy()
    if audit == "policy":
        _, started = await started_run(
            database_session_factory,
            workflow_version=WorkflowVersion.V2,
            model_budget=policy,
        )
        async with database_session_factory() as session, session.begin():
            await session.execute(
                update(InvestigationRunRow)
                .where(InvestigationRunRow.id == started.run.id)
                .values(context_policy_version="evidence-read-v1")
            )
    else:
        _, claim, _, _, attempts, store = await prepare(
            database_session_factory,
            model_budget=policy,
        )
        grant = await reserve_slot(
            store,
            claim,
            attempts["log"],
            0,
            operation=CallOperation.ANALYZE_LOG,
            model_input=model_input(method="function_calling"),
            prompt_version="evidence-read-v1",
        )
        async with database_session_factory() as session, session.begin():
            statement = text(
                "UPDATE investigation_call_reservations SET "
                "model_input = jsonb_set(model_input, '{version}', "
                "'\"evidence-loop-input-v1\"'::jsonb) WHERE id = :call_id"
            )
            if audit == "interaction":
                statement = text(
                    "UPDATE investigation_call_reservations SET status = 'succeeded', "
                    "finished_at = reserved_at, "
                    "model_input = jsonb_set(model_input, '{version}', "
                    "'\"evidence-loop-input-v1\"'::jsonb), "
                    'call_result = \'{"kind": "model"}\'::jsonb '
                    "WHERE id = :call_id"
                )
            await session.execute(statement, {"call_id": grant.record.id})

    with pytest.raises(DBAPIError, match="不能删除"):
        await asyncio.to_thread(command.downgrade, config(), "0037_call_invocation_index")
    async with database_session_factory() as session:
        assert await session.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0083_candidate_recovery"
        )
