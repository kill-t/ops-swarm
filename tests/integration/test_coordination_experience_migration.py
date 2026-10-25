"""真实 PostgreSQL 验证协调经验上下文的启动、冻结与调用边界。"""

import asyncio
import json
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from alembic import command
from opsswarm.control_plane.schemas import StartInvestigationRequest
from opsswarm.experience.context_contracts import ExperienceContextSelection
from opsswarm.experience.contracts import canonical_hash
from opsswarm.incidents import ActorType, AlertSubmission, IncidentActor, IncidentSeverity
from opsswarm.incidents.persistence import IncidentStore
from opsswarm.investigation.models import InvestigationMode
from opsswarm.investigation.persistence import InvestigationStore
from opsswarm.investigation.workflow_versions import WorkflowVersion
from tests.loop_helpers import loop_policy
from tests.task_facts_helpers import BASE_TIME

pytestmark = pytest.mark.integration


def config() -> Config:
    return Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))


def test_start_request_omission_is_legacy_but_explicit_null_is_rejected() -> None:
    fields = {
        "expected_version": 1,
        "reason": "验证启动契约",
        "mode": "multi_agent",
        "workflow_version": "v3",
        "model_budget": loop_policy(),
    }
    request = StartInvestigationRequest.model_validate(fields)
    assert request.coordination_experience_policy_version is None

    with pytest.raises(ValidationError, match="省略时禁用"):
        StartInvestigationRequest.model_validate(
            {**fields, "coordination_experience_policy_version": None}
        )


async def start_v3_run(database_session_factory, *, experience: bool):
    key = str(uuid4())
    incident = await IncidentStore(database_session_factory).ingest_alert(
        AlertSubmission(
            source="coordination-experience-migration",
            external_event_id=key,
            fingerprint=key,
            service_name="order-service",
            severity=IncidentSeverity.P1,
            title="协调经验迁移测试",
            payload={},
            occurred_at=BASE_TIME,
            ingested_at=BASE_TIME,
        )
    )
    store = InvestigationStore(database_session_factory)
    started = await store.start_investigation(
        incident.incident.id,
        expected_version=1,
        occurred_at=BASE_TIME + timedelta(seconds=1),
        actor=IncidentActor(actor_type=ActorType.HUMAN, actor_id="migration-tester"),
        reason="验证协调经验上下文迁移",
        mode=InvestigationMode.MULTI_AGENT,
        workflow_version=WorkflowVersion.V3,
        model_budget=loop_policy(),
        coordination_experience_policy_version=(
            "coordination-experience-v1" if experience else None
        ),
    )
    return store, started


async def claim_v3(store: InvestigationStore):
    claim = await store.claim_next(
        worker_id=str(uuid4()),
        occurred_at=BASE_TIME + timedelta(seconds=2),
        lease_duration=timedelta(seconds=30),
        supported_workflow_versions=(WorkflowVersion.V3,),
    )
    assert claim is not None
    return claim


def empty_selection(started) -> ExperienceContextSelection:
    return ExperienceContextSelection.model_validate(
        {
            "id": uuid4(),
            "scope": {
                "run_id": started.run.id,
                "incident_id": started.run.incident_id,
                "service_name": "order-service",
                "environment": "local_fault_lab",
                "data_source_mode": "frozen",
            },
            "selected_at": BASE_TIME + timedelta(seconds=2),
            "items": [],
        }
    )


def experience_input(selection: ExperienceContextSelection) -> dict:
    content = json.dumps(
        {"historical_experience": selection.model_dump(mode="json")},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return {
        "version": "coordination-experience-input-v1",
        "messages": [
            {"role": "system", "content": "仅将历史经验作为调查参考。"},
            {"role": "user", "content": content},
        ],
    }


def experience_manifest(selection: ExperienceContextSelection) -> dict:
    return {
        "version": "coordination-experience-v1",
        "prompt_version": "coordination-experience-plan-v1",
        "materials": [
            {"kind": "incident_input"},
            {"kind": "coordination_policy"},
            {
                "kind": "experience_selection",
                "material_id": str(selection.id),
                "version": selection.policy_version,
                "content_sha256": canonical_hash(selection),
            },
        ],
    }


async def insert_context(session, claim, selection, first_call_id: UUID, *, content=None) -> None:
    await session.execute(
        text(
            "INSERT INTO investigation_experience_contexts "
            "(investigation_run_id,id,policy_version,first_call_id,content,content_hash,"
            "created_at) "
            "VALUES (:run_id,:id,'coordination-experience-v1',:first_call_id,"
            "CAST(:content AS jsonb),:content_hash,:created_at)"
        ),
        {
            "run_id": claim.run.id,
            "id": selection.id,
            "first_call_id": first_call_id,
            "content": json.dumps(
                selection.model_dump(mode="json") if content is None else content
            ),
            "content_hash": canonical_hash(selection),
            "created_at": BASE_TIME + timedelta(seconds=3),
        },
    )


async def insert_coordinate_call(
    session,
    claim,
    selection: ExperienceContextSelection,
    *,
    call_id: UUID,
    attempt_id: UUID | None = None,
    sequence: int = 1,
    operation: str = "coordinate",
    manifest: dict | None = None,
    model_input: dict | None = None,
) -> None:
    parent_attempt_id = attempt_id or claim.attempt.id
    await session.execute(
        text(
            "INSERT INTO investigation_call_reservations "
            "(id,investigation_run_id,investigation_attempt_id,task_id,task_attempt_id,"
            "sequence,invocation_index,idempotency_key,operation,kind,status,error_code,"
            "reserved_at,finished_at,context_manifest,model_input,model_allocation,"
            "model_usage,tool_request,call_result) VALUES "
            "(:id,:run_id,:attempt_id,NULL,NULL,:sequence,0,:key,:operation,'model',"
            "'reserved',NULL,:reserved_at,NULL,CAST(:manifest AS jsonb),"
            "CAST(:model_input AS jsonb),'{}'::jsonb,NULL,NULL,NULL)"
        ),
        {
            "id": call_id,
            "run_id": claim.run.id,
            "attempt_id": parent_attempt_id,
            "sequence": sequence,
            "key": f"{parent_attempt_id}:root:{operation}",
            "operation": operation,
            "reserved_at": BASE_TIME + timedelta(seconds=3),
            "manifest": json.dumps(manifest or experience_manifest(selection)),
            "model_input": json.dumps(model_input or experience_input(selection)),
        },
    )


async def insert_retry_attempt(session, claim, *, attempt_number: int) -> UUID:
    attempt_id = uuid4()
    await session.execute(
        text(
            "INSERT INTO investigation_attempts "
            "(id,investigation_run_id,work_item_id,attempt_number,lease_token,status,worker_id,"
            "model_name,input_cost_per_million_usd,output_cost_per_million_usd,"
            "model_request_count,usage_complete,input_tokens,output_tokens,total_tokens,"
            "estimated_cost_usd,started_at,lease_expires_at,finished_at,duration_ms,will_retry,"
            "error_code,error_message) VALUES "
            "(:id,:run_id,:work_item_id,:attempt_number,:lease_token,'running','retry-worker',"
            "NULL,NULL,NULL,"
            "0,false,0,0,0,NULL,:started_at,:lease_expires_at,NULL,NULL,NULL,NULL,NULL)"
        ),
        {
            "id": attempt_id,
            "run_id": claim.run.id,
            "work_item_id": claim.work_item.id,
            "attempt_number": attempt_number,
            "lease_token": uuid4(),
            "started_at": BASE_TIME + timedelta(seconds=4),
            "lease_expires_at": BASE_TIME + timedelta(seconds=34),
        },
    )
    return attempt_id


@pytest.mark.asyncio
async def test_0066_metadata_and_empty_downgrade_upgrade(database_session_factory) -> None:
    await asyncio.to_thread(command.check, config())
    await asyncio.to_thread(command.downgrade, config(), "0065_experience_replacement")
    try:
        async with database_session_factory() as session:
            assert (
                await session.scalar(text("SELECT version_num FROM alembic_version"))
                == "0065_experience_replacement"
            )
        await asyncio.to_thread(command.upgrade, config(), "head")
        await asyncio.to_thread(command.check, config())
    finally:
        await asyncio.to_thread(command.upgrade, config(), "head")


@pytest.mark.asyncio
async def test_0066_start_opt_in_is_explicit_queryable_and_frozen(
    database_session_factory,
) -> None:
    store, started = await start_v3_run(database_session_factory, experience=True)
    persisted = await store.get_run(started.run.id)
    assert persisted.coordination_experience_policy_version == "coordination-experience-v1"

    _, legacy = await start_v3_run(database_session_factory, experience=False)
    assert legacy.run.coordination_experience_policy_version is None

    with pytest.raises(DBAPIError, match="不可修改"):
        async with database_session_factory() as session, session.begin():
            await session.execute(
                text(
                    "UPDATE investigation_runs SET coordination_experience_policy_version=NULL "
                    "WHERE id=:run_id"
                ),
                {"run_id": started.run.id},
            )

    invalid_store, invalid = await start_v3_run(database_session_factory, experience=False)
    with pytest.raises(ValueError, match="只支持当前冻结 v3"):
        await invalid_store.start_investigation(
            invalid.run.incident_id,
            expected_version=2,
            occurred_at=BASE_TIME + timedelta(seconds=3),
            actor=IncidentActor(actor_type=ActorType.HUMAN, actor_id="migration-tester"),
            reason="非法版本不会进入事务",
            mode=InvestigationMode.MULTI_AGENT,
            workflow_version=WorkflowVersion.V2,
            model_budget=loop_policy(),
            coordination_experience_policy_version="coordination-experience-v1",
        )


@pytest.mark.asyncio
async def test_0066_snapshot_is_native_bounded_immutable_and_deferred_to_same_run(
    database_session_factory,
) -> None:
    store, started = await start_v3_run(database_session_factory, experience=True)
    claim = await claim_v3(store)
    selection = empty_selection(started)
    first_call_id = uuid4()
    async with database_session_factory() as session, session.begin():
        await insert_context(session, claim, selection, first_call_id)
        await insert_coordinate_call(session, claim, selection, call_id=first_call_id)

    async with database_session_factory() as session:
        definition = await session.scalar(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname='fk_investigation_experience_contexts_first_call'"
            )
        )
    assert "DEFERRABLE INITIALLY DEFERRED" in definition

    cross_run_call_id = uuid4()
    async with database_session_factory() as session, session.begin():
        retry_attempt_id = await insert_retry_attempt(session, claim, attempt_number=2)
        await insert_coordinate_call(
            session,
            claim,
            selection,
            call_id=cross_run_call_id,
            attempt_id=retry_attempt_id,
            sequence=2,
        )

    with pytest.raises(DBAPIError, match="不可修改"):
        async with database_session_factory() as session, session.begin():
            await session.execute(
                text(
                    "UPDATE investigation_experience_contexts SET content_hash=:hash "
                    "WHERE investigation_run_id=:run_id"
                ),
                {"hash": "a" * 64, "run_id": claim.run.id},
            )
    with pytest.raises(DBAPIError, match="不可修改"):
        async with database_session_factory() as session, session.begin():
            await session.execute(
                text(
                    "DELETE FROM investigation_experience_contexts "
                    "WHERE investigation_run_id=:run_id"
                ),
                {"run_id": claim.run.id},
            )

    second_store, second_started = await start_v3_run(database_session_factory, experience=True)
    second_claim = await claim_v3(second_store)
    second_selection = empty_selection(second_started)
    with pytest.raises(DBAPIError):
        async with database_session_factory() as session, session.begin():
            await insert_context(session, second_claim, second_selection, cross_run_call_id)

    malformed_id = uuid4()
    with pytest.raises(DBAPIError):
        async with database_session_factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO investigation_experience_contexts "
                    "(investigation_run_id,id,policy_version,first_call_id,content,"
                    "content_hash,created_at) VALUES "
                    "(:run_id,:id,'coordination-experience-v1',:call_id,'{}'::jsonb,"
                    "'not-a-hash',:created_at)"
                ),
                {
                    "run_id": second_claim.run.id,
                    "id": malformed_id,
                    "call_id": uuid4(),
                    "created_at": BASE_TIME + timedelta(seconds=3),
                },
            )


@pytest.mark.asyncio
async def test_0066_every_coordinate_call_binds_the_frozen_selection(
    database_session_factory,
) -> None:
    store, started = await start_v3_run(database_session_factory, experience=True)
    claim = await claim_v3(store)
    selection = empty_selection(started)
    first_call_id = uuid4()
    async with database_session_factory() as session, session.begin():
        await insert_context(session, claim, selection, first_call_id)
        await insert_coordinate_call(session, claim, selection, call_id=first_call_id)

    async with database_session_factory() as session, session.begin():
        retry_attempt_id = await insert_retry_attempt(session, claim, attempt_number=2)
        retry_call_id = uuid4()
        await insert_coordinate_call(
            session,
            claim,
            selection,
            call_id=retry_call_id,
            attempt_id=retry_attempt_id,
            sequence=2,
        )
    assert retry_call_id != first_call_id

    wrong_selection = selection.model_copy(update={"id": uuid4()})
    with pytest.raises(DBAPIError, match="未精确绑定"):
        async with database_session_factory() as session, session.begin():
            third_attempt_id = await insert_retry_attempt(session, claim, attempt_number=3)
            await insert_coordinate_call(
                session,
                claim,
                selection,
                call_id=uuid4(),
                attempt_id=third_attempt_id,
                sequence=3,
                manifest=experience_manifest(wrong_selection),
            )


@pytest.mark.asyncio
async def test_0066_call_contract_follows_run_opt_in(database_session_factory) -> None:
    legacy_store, legacy_started = await start_v3_run(database_session_factory, experience=False)
    legacy_claim = await claim_v3(legacy_store)
    selection = empty_selection(legacy_started)
    with pytest.raises(DBAPIError, match="必须使用旧输入契约"):
        async with database_session_factory() as session, session.begin():
            await insert_coordinate_call(
                session,
                legacy_claim,
                selection,
                call_id=uuid4(),
            )

    store, started = await start_v3_run(database_session_factory, experience=True)
    claim = await claim_v3(store)
    selection = empty_selection(started)
    first_call_id = uuid4()
    old_input = {"version": "coordination-model-input-v1"}
    with pytest.raises(DBAPIError, match="调用契约不匹配"):
        async with database_session_factory() as session, session.begin():
            await insert_context(session, claim, selection, first_call_id)
            await insert_coordinate_call(
                session,
                claim,
                selection,
                call_id=first_call_id,
                model_input=old_input,
            )


@pytest.mark.asyncio
async def test_0066_downgrade_refuses_any_enabled_run(database_session_factory) -> None:
    _, started = await start_v3_run(database_session_factory, experience=True)
    with pytest.raises(DBAPIError, match="不能删除已保存"):
        await asyncio.to_thread(command.downgrade, config(), "0065_experience_replacement")
    async with database_session_factory() as session:
        assert (
            await session.scalar(text("SELECT version_num FROM alembic_version"))
            == "0078_condition_approval"
        )
        assert (
            await session.scalar(
                text(
                    "SELECT coordination_experience_policy_version FROM investigation_runs "
                    "WHERE id=:run_id"
                ),
                {"run_id": started.run.id},
            )
            == "coordination-experience-v1"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["missing_items", "null_items", "many_items", "missing_scope"])
async def test_0066_required_json_fields_cannot_bypass_checks_with_sql_null(
    database_session_factory, invalid
):
    store, started = await start_v3_run(database_session_factory, experience=True)
    claim = await claim_v3(store)
    selection = empty_selection(started)
    content = selection.model_dump(mode="json")
    if invalid == "missing_items":
        del content["items"]
    elif invalid == "null_items":
        content["items"] = None
    elif invalid == "many_items":
        content["items"] = [{}, {}, {}, {}]
    else:
        del content["scope"]
    with pytest.raises(DBAPIError):
        async with database_session_factory() as session, session.begin():
            await insert_context(session, claim, selection, uuid4(), content=content)


@pytest.mark.asyncio
async def test_0066_deferred_first_source_must_be_a_coordinate_call(database_session_factory):
    store, started = await start_v3_run(database_session_factory, experience=True)
    claim = await claim_v3(store)
    selection = empty_selection(started)
    call_id = uuid4()
    with pytest.raises(DBAPIError, match="根级协调预留"):
        async with database_session_factory() as session, session.begin():
            await insert_context(session, claim, selection, call_id)
            await insert_coordinate_call(
                session, claim, selection, call_id=call_id, operation="synthesize"
            )
