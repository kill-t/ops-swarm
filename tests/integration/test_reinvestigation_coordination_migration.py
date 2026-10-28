"""真实 PostgreSQL 核验回流冻结输入、独立归属和不可变发布闭环。"""

import asyncio
import copy
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from alembic import command
from opsswarm.approval.conditions.queries import raw_content_hash
from opsswarm.investigation.budget.model_contracts import ModelInput
from opsswarm.investigation.budget.queries import read_budget_snapshot
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.investigation.reinvestigation.tables import CandidateReinvestigationRow
from opsswarm.investigation.reinvestigation_coordination.context import build_packet
from opsswarm.investigation.reinvestigation_coordination.contracts import ReflowError
from opsswarm.investigation.reinvestigation_coordination.source import load_source
from tests.api import test_reinvestigation_coordination_api as api
from tests.api.test_reinvestigation_coordination_api import (
    current_fixture_time as current_fixture_time,
)
from tests.api.test_reinvestigation_coordination_api import (
    initial_reflow_capacity as initial_reflow_capacity,
)
from tests.api.test_reinvestigation_coordination_api import wait_context as wait_context
from tests.integration.test_candidate_approval_migration import config, encoded

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def insert(session, table, values):
    await session.execute(
        text(
            f"INSERT INTO {table} SELECT * FROM jsonb_populate_record(NULL::{table}, "
            "CAST(:values AS jsonb))"
        ),
        {"values": encoded(values)},
    )


async def prepared_values(c):
    async with c.db() as session:
        run = await session.get(InvestigationRunRow, c.started.run.id)
        admission = await session.get(CandidateReinvestigationRow, run.id)
        now = c.clock()
        source = await load_source(session, run, admission, created_at=now, clock=c.clock)
        packet = build_packet(source)
        native_source = await session.scalar(
            text("SELECT reflow_source(:run,:now)"), {"run": run.id, "now": now}
        )
        assert native_source == source.model_dump(mode="json")
        native_packet = await session.scalar(
            text("SELECT reflow_packet(CAST(:source AS jsonb))"),
            {"source": encoded(native_source)},
        )
        assert native_packet == packet.model_dump(mode="json")
        return dict(
            id=str(run.id),
            investigation_run_id=str(run.id),
            policy_version="reinvestigation-coordination-v1",
            source=source.model_dump(mode="json"),
            source_sha256=content_hash(source),
            packet=packet.model_dump(mode="json"),
            packet_sha256=content_hash(packet),
            status="queued",
            attempt_count=0,
            available_at=now.isoformat(),
            created_at=now.isoformat(),
            updated_at=now.isoformat(),
            completed_at=None,
            locked_by=None,
            lease_token=None,
            locked_until=None,
            last_error=None,
        )


async def test_0087_metadata_and_empty_roundtrip(database_session_factory):
    await asyncio.to_thread(command.check, config())
    try:
        await asyncio.to_thread(command.downgrade, config(), "0086_reinvestigation_admission")
        async with database_session_factory() as session:
            assert (
                await session.scalar(text("SELECT to_regclass('investigation_reflow_jobs')"))
                is None
            )
    finally:
        await asyncio.to_thread(command.upgrade, config(), "head")
    await asyncio.to_thread(command.check, config())


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_0087_rehashed_incomplete_source_packet_and_false_blocker_are_rejected(wait_context):
    c = wait_context
    await api.prepare(c)
    values = await prepared_values(c)
    variants = []
    for mutation in ("material", "reference", "round", "task", "manifest", "input", "allocation"):
        forged = copy.deepcopy(values)
        if mutation == "material":
            forged["source"]["materials"][0]["content"]["failed_calls"] = []
            forged["source"]["materials"][0]["content"]["candidate_source"] = {}
        elif mutation == "reference":
            forged["source"]["materials"][6]["reference_id"] = str(uuid4())
        elif mutation == "round":
            forged["source"]["next_round_number"] = 1
        elif mutation == "task":
            forged["source"]["existing_task_count"] = 0
        elif mutation == "manifest":
            forged["packet"]["manifest"]["materials"] = []
        elif mutation == "input":
            forged["packet"]["model_input"]["messages"][1]["content"] = "{}"
        else:
            forged["packet"]["allocation"]["estimated_input_tokens"] = 1
        forged["source_sha256"] = raw_content_hash(forged["source"])
        forged["packet_sha256"] = raw_content_hash(forged["packet"])
        variants.append(forged)
    variants.append(
        {
            **values,
            "status": "blocked",
            "source": None,
            "source_sha256": None,
            "packet": None,
            "packet_sha256": None,
            "completed_at": values["created_at"],
            "last_error": "reflow_source_changed",
        }
    )
    for forged in variants:
        with pytest.raises(DBAPIError):
            async with c.db() as session, session.begin():
                await insert(session, "investigation_reflow_jobs", forged)
    async with c.db() as session, session.begin():
        await insert(session, "investigation_reflow_jobs", values)


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_0087_native_call_owner_and_false_result_are_rejected(wait_context):
    c = wait_context
    await api.prepare(c)
    before = await api.root_facts(c)
    original_calls = await api.calls(c)
    assert await c.reflow_store.prepare_next()
    with pytest.raises(DBAPIError, match="连续尝试"):
        async with c.db() as session, session.begin():
            await session.execute(
                text(
                    "UPDATE investigation_reflow_jobs SET status='running',attempt_count=1,"
                    "locked_by='forged',lease_token=:token,"
                    "locked_until=clock_timestamp()+interval '120 seconds',"
                    "updated_at=clock_timestamp()"
                ),
                {"token": uuid4()},
            )
    claims = await asyncio.gather(
        *(
            c.reflow_store.claim_next(
                worker_id=f"native-132-{i}", lease_duration=timedelta(seconds=30)
            )
            for i in range(2)
        )
    )
    assert sum(item is not None for item in claims) == 1
    claim = next(item for item in claims if item is not None)
    grant = await c.reflow_ledger.reserve(claim)
    async with c.db() as session:
        call = await session.scalar(
            text("SELECT to_jsonb(t) FROM investigation_call_reservations t WHERE id=:id"),
            {"id": grant.record.id},
        )
    async with c.db() as session:
        run = await session.get(InvestigationRunRow, c.started.run.id)
        native_budget = await session.scalar(
            text("SELECT reflow_budget_snapshot(:id,clock_timestamp())"), {"id": run.id}
        )
        assert native_budget == (await read_budget_snapshot(session, run)).model_dump(mode="json")
    for field, value in (
        ("reinvestigation_attempt_id", str(uuid4())),
        ("investigation_attempt_id", str(uuid4())),
        ("candidate_attempt_id", str(uuid4())),
        ("model_input", {}),
        ("context_manifest", {}),
    ):
        forged = {**call, "id": str(uuid4()), "sequence": call["sequence"] + 1, field: value}
        with pytest.raises(DBAPIError):
            async with c.db() as session, session.begin():
                await insert(session, "investigation_call_reservations", forged)
    now = c.clock()
    content = dict(
        investigation_run_id=str(claim.investigation_run_id),
        job_id=str(claim.job_id),
        source_call_id=str(grant.record.id),
        source_sha256="a" * 64,
        proposal={},
        dispatch_status="not_dispatched",
        created_at=now.isoformat(),
    )
    with pytest.raises(DBAPIError):
        async with c.db() as session, session.begin():
            await insert(
                session,
                "investigation_reflow_results",
                dict(
                    job_id=str(claim.job_id),
                    investigation_run_id=str(claim.investigation_run_id),
                    attempt_id=str(claim.attempt_id),
                    source_call_id=str(grant.record.id),
                    content=content,
                    content_sha256=raw_content_hash(content),
                    created_at=now.isoformat(),
                ),
            )
    # SQL结算发生在租约内，刻意持事务至真实超期，延迟门禁必须整体回滚成功调用。
    proposal = await api.FakeModel().invoke(ModelInput.model_validate(call["model_input"]))
    with pytest.raises(DBAPIError, match="调用提交前"):
        async with c.db() as session, session.begin():
            await session.execute(
                text(
                    "UPDATE investigation_call_reservations SET status='succeeded',"
                    "finished_at=clock_timestamp(),call_result=CAST(:result AS jsonb) WHERE id=:id"
                ),
                {"id": grant.record.id, "result": encoded(proposal.model_dump(mode="json"))},
            )
            # 宿主机与容器时钟可能有偏差，提交门禁的真实时刻以PostgreSQL为准。
            while (await session.scalar(text("SELECT clock_timestamp()"))) < claim.lease_expires_at:
                await asyncio.sleep(0.5)
    with pytest.raises(ReflowError, match="租约"):
        await c.reflow_ledger.settle(claim, grant.record.id, status="unknown", usage=None)
    with pytest.raises(ReflowError, match="租约"):
        await c.reflow_store.complete(claim, grant.record.id)
    replacement = await c.reflow_store.claim_next(worker_id="native-takeover-132")
    assert replacement is not None and replacement.number == 2
    taken_over = await api.calls(c)
    assert len(taken_over) == len(original_calls) + 1
    assert taken_over[-1]["status"] == "unknown" and taken_over[-1]["model_usage"] is None
    assert taken_over[-1]["reinvestigation_attempt_id"] == str(claim.attempt_id)
    assert taken_over[-1]["model_allocation"] == call["model_allocation"]
    new_grant = await c.reflow_ledger.reserve(replacement)
    assert (
        new_grant.record.id != grant.record.id and new_grant.record.sequence == call["sequence"] + 1
    )
    after = await api.calls(c)
    assert after[:-1] == taken_over and len(after) == len(original_calls) + 2
    assert await api.root_facts(c) == before
    await c.reflow_store.fail(replacement, error_code="native_test_finished", retryable=False)
    assert await api.root_facts(c) == before


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_0087_successful_job_attempt_call_and_result_are_immutable_and_block_downgrade(
    wait_context,
):
    c = wait_context
    await api.prepare(c)
    assert await api.worker(c, api.FakeModel()).run_once()
    response = await api.get(c)
    assert response.status_code == 200 and response.json()["status"] == "succeeded", response.text
    for table in (
        "investigation_reflow_jobs",
        "investigation_reflow_attempts",
        "investigation_reflow_results",
    ):
        for sql in (f"DELETE FROM {table}", f"UPDATE {table} SET created_at=created_at"):
            # 尝试表使用started_at；即使无实际字段变动也不能改写终态。
            sql = sql.replace("created_at", "started_at") if table.endswith("attempts") else sql
            with pytest.raises(DBAPIError):
                async with c.db() as session, session.begin():
                    await session.execute(text(sql))
    with pytest.raises(DBAPIError):
        async with c.db() as session, session.begin():
            await session.execute(
                text(
                    "UPDATE investigation_call_reservations SET status='unknown', "
                    "error_code='forged' WHERE operation='coordinate_reinvestigation'"
                )
            )
    with pytest.raises(DBAPIError):
        await asyncio.to_thread(command.downgrade, config(), "0086_reinvestigation_admission")
    await asyncio.to_thread(command.check, config())


async def test_0087_native_proposal_requires_strict_shape_and_exact_round(database_session_factory):
    source = {"next_round_number": 2, "coordination_policy": {"max_rounds": 3}, "maximum_tasks": 2}
    proposal = {
        "version": "coordination-proposal-v1",
        "round_number": 2,
        "goal": "继续调查",
        "tasks": [
            {"task_key": "check_log", "role": "log", "goal": "读取新日志", "lookback_seconds": 300}
        ],
    }
    variants = []
    for mutation in (
        "round",
        "float_round",
        "space_goal",
        "bool_key",
        "float_lookback",
        "unknown_field",
        "duplicate_role",
        "capacity",
    ):
        forged = copy.deepcopy(proposal)
        if mutation == "round":
            forged["round_number"] = 1
        elif mutation == "float_round":
            forged["round_number"] = 2.0
        elif mutation == "space_goal":
            forged["goal"] = " "
        elif mutation == "bool_key":
            forged["tasks"][0]["task_key"] = False
        elif mutation == "float_lookback":
            forged["tasks"][0]["lookback_seconds"] = 300.0
        elif mutation == "unknown_field":
            forged["tasks"][0]["command"] = "forbidden"
        elif mutation == "duplicate_role":
            forged["tasks"].append({**forged["tasks"][0], "task_key": "check_other"})
        else:
            forged["tasks"] *= 3
        variants.append(forged)
    for forged in variants:
        with pytest.raises(DBAPIError):
            async with database_session_factory() as session, session.begin():
                await session.execute(
                    text(
                        "SELECT assert_reflow_proposal(CAST(:proposal AS jsonb), "
                        "CAST(:source AS jsonb))"
                    ),
                    {"proposal": encoded(forged), "source": encoded(source)},
                )
    async with database_session_factory() as session:
        await session.execute(
            text("SELECT assert_reflow_proposal(CAST(:proposal AS jsonb), CAST(:source AS jsonb))"),
            {"proposal": encoded(proposal), "source": encoded(source)},
        )
