"""真实 PostgreSQL 验证候选任务、可信准入与原共享账本原生护栏。"""

import asyncio
import hashlib
import json
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from alembic import command
from opsswarm.investigation.candidate.contracts import EvaluateCandidateCommand
from tests.integration.test_coordination_experience_migration import claim_v3, start_v3_run
from tests.task_facts_helpers import BASE_TIME

pytestmark = pytest.mark.integration


def config() -> Config:
    return Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))


async def insert_refusal(
    factory, *, invalid=None, raw_override=None, command_override=None
) -> None:
    """拒绝也须绑定实际可信准入；未知运行不要求伪造业务行。"""
    run_id, authorization_id = uuid4(), uuid4()
    body = {
        "action": "evaluate_candidate",
        "source_kind": "terminal_handoff",
        "source_id": str(uuid4()),
        "source_sha256": "a" * 64,
        "expected_wait_version": None,
        "idempotency_key": "migration-candidate",
        "reason": "核验当前候选来源与完整预算",
    }
    if command_override is not None:
        body = dict(command_override)
    canonical = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if isinstance(raw_override, str):
        raw = raw_override
    else:
        raw = json.dumps(
            raw_override if raw_override is not None else body,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    values = {
        "run": run_id,
        "authorization": authorization_id,
        "subject": "migration-engineer",
        "role": "viewer" if invalid == "role" else "engineer",
        "operation": "reassess_investigation_human_input"
        if invalid == "operation"
        else "evaluate_investigation_candidate",
        "resources": json.dumps({"run_id": str(uuid4() if invalid == "resources" else run_id)}),
        "body_hash": "b" * 64
        if invalid == "body_hash"
        else hashlib.sha256(raw.encode()).hexdigest(),
        "raw": raw,
        "command_hash": "c" * 64 if invalid == "command_hash" else digest,
        "now": BASE_TIME,
    }
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO operator_authorizations (id,policy_version,subject_id,credential_id,"
                "role,permissions,operation,method,route,resources,body_sha256,authorized_at) "
                "VALUES (:authorization,'operator-auth-v1',:subject,'migration',:role,"
                "'[\"read\",\"manage_incident\"]'::jsonb,:operation,'POST',"
                "'/api/v1/investigations/{run_id}/candidate-evaluations',"
                "CAST(:resources AS jsonb),:body_hash,:now)"
            ),
            values,
        )
        if invalid == "subject":
            values["subject"] = "another-engineer"
        elif invalid == "action":
            body["action"] = "execute"
        elif invalid == "source_kind":
            body["source_kind"] = "arbitrary_source"
        elif invalid == "wait_version":
            body["expected_wait_version"] = 2
        elif invalid == "extra_field":
            body["execution_eligible"] = True
        values["command"] = json.dumps(body, ensure_ascii=False)
        await session.execute(
            text(
                "INSERT INTO investigation_candidate_evaluation_commands (authorization_id,"
                "investigation_run_id,subject_id,command,raw_body,command_sha256,outcome,"
                "http_status,error_code,error_message,created_at) "
                "VALUES (:authorization,:run,:subject,CAST(:command AS jsonb),:raw,"
                ":command_hash,'error',404,'candidate_run_not_found','目标运行不存在',:now)"
            ),
            values,
        )


@pytest.mark.asyncio
async def test_0072_metadata_and_empty_roundtrip(database_session_factory) -> None:
    await asyncio.to_thread(command.check, config())
    try:
        await asyncio.to_thread(command.downgrade, config(), "0071_human_reassessment_routing")
        async with database_session_factory() as session:
            assert await session.scalar(text("SELECT version_num FROM alembic_version")) == (
                "0071_human_reassessment_routing"
            )
            assert (
                await session.scalar(
                    text("SELECT to_regclass('investigation_candidate_evaluation_jobs')")
                )
                is None
            )
            constraints = dict(
                (
                    await session.execute(
                        text(
                            "SELECT conname,pg_get_constraintdef(oid) FROM pg_constraint "
                            "WHERE conname IN ('ck_call_reservations_operation',"
                            "'ck_call_reservations_human_owner')"
                        )
                    )
                ).all()
            )
            assert "evaluate_candidate" not in constraints["ck_call_reservations_operation"]
            assert "human_review" in constraints["ck_call_reservations_human_owner"]
        await asyncio.to_thread(command.upgrade, config(), "head")
        await asyncio.to_thread(command.check, config())
    finally:
        await asyncio.to_thread(command.upgrade, config(), "head")


@pytest.mark.asyncio
async def test_0072_guards_cover_candidate_facts_and_ledger(database_session_factory) -> None:
    expected = {
        "candidate_evaluation_jobs_guard": False,
        "candidate_evaluation_attempts_guard": False,
        "candidate_evaluation_results_guard": False,
        "candidate_evaluation_commands_guard": False,
        "candidate_evaluation_calls_guard": False,
        "candidate_evaluation_jobs_state": True,
        "candidate_evaluation_attempts_state": True,
        "candidate_evaluation_results_state": True,
    }
    async with database_session_factory() as session:
        guards = dict(
            (
                await session.execute(
                    text(
                        "SELECT tgname,tgdeferrable FROM pg_trigger WHERE "
                        "tgname LIKE 'candidate_evaluation_%'"
                    )
                )
            ).all()
        )
        assert guards == expected
        constraints = dict(
            (
                await session.execute(
                    text(
                        "SELECT conname,pg_get_constraintdef(oid) FROM pg_constraint "
                        "WHERE conname IN ('fk_call_reservations_candidate_attempt',"
                        "'ck_call_reservations_candidate_owner','ck_call_reservations_human_owner',"
                        "'ck_call_reservations_invocation_key','ck_call_reservations_operation',"
                        "'ck_call_reservations_scope','ck_call_reservations_loop_payload')"
                    )
                )
            ).all()
        )
        assert (
            "candidate_attempt_id, investigation_run_id"
            in (constraints["fk_call_reservations_candidate_attempt"])
        )
        assert "evaluate_candidate" in constraints["ck_call_reservations_candidate_owner"]
        assert "human_review" in constraints["ck_call_reservations_human_owner"]
        assert "candidate:" in constraints["ck_call_reservations_invocation_key"]
        assert "evaluate_candidate" in constraints["ck_call_reservations_operation"]
        assert "evaluate_candidate" in constraints["ck_call_reservations_scope"]
        assert "candidate-model-result-v1" in constraints["ck_call_reservations_loop_payload"]


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["role", "operation", "resources", "body_hash", "subject"])
async def test_0072_refusal_audit_rejects_forged_admission(
    database_session_factory, invalid
) -> None:
    with pytest.raises(DBAPIError, match="候选评估命令缺少精确原始正文"):
        await insert_refusal(database_session_factory, invalid=invalid)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid", ["action", "source_kind", "wait_version", "extra_field", "command_hash"]
)
async def test_0072_refusal_audit_requires_exact_canonical_command(
    database_session_factory, invalid
) -> None:
    with pytest.raises(DBAPIError, match="有界精确规范来源绑定"):
        await insert_refusal(database_session_factory, invalid=invalid)


def canonical_command() -> dict:
    return {
        "action": "evaluate_candidate",
        "source_kind": "terminal_handoff",
        "source_id": "12345678-1234-5678-abcd-123456789abc",
        "source_sha256": "a" * 64,
        "expected_wait_version": None,
        "idempotency_key": "migration-candidate",
        "reason": "核验当前候选来源与完整预算",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["reason", "idempotency_key", "source_id", "source_sha256"])
async def test_0072_audit_binds_raw_body_to_same_canonical_command(
    database_session_factory, field
) -> None:
    raw = canonical_command()
    altered = dict(raw)
    altered[field] = {
        "reason": "授权原始正文没有请求该理由",
        "idempotency_key": "another-command-key",
        "source_id": str(uuid4()),
        "source_sha256": "b" * 64,
    }[field]
    # 两份命令各自合法，各自散列也正确；仍必须拒绝借用另一份正文的准入。
    EvaluateCandidateCommand.model_validate(raw)
    EvaluateCandidateCommand.model_validate(altered)
    with pytest.raises(DBAPIError, match="原始正文与规范命令不一致"):
        await insert_refusal(database_session_factory, raw_override=raw, command_override=altered)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "normalization",
    ["default_action", "ascii_trim", "unicode_trim", "upper_uuid", "compact_uuid", "braces", "urn"],
)
async def test_0072_audit_accepts_actual_pydantic_normalization(
    database_session_factory, normalization
) -> None:
    raw = canonical_command()
    if normalization == "default_action":
        raw.pop("action")
    elif normalization in {"ascii_trim", "unicode_trim"}:
        whitespace = " \t\r\n" if normalization == "ascii_trim" else "\u3000\u00a0\u2003"
        for field in ("source_sha256", "idempotency_key", "reason"):
            raw[field] = whitespace + raw[field] + whitespace
    elif normalization == "upper_uuid":
        raw["source_id"] = raw["source_id"].upper()
    elif normalization == "compact_uuid":
        raw["source_id"] = raw["source_id"].replace("-", "")
    elif normalization == "braces":
        raw["source_id"] = "{" + raw["source_id"] + "}"
    elif normalization == "urn":
        raw["source_id"] = "urn:uuid:" + raw["source_id"]
    normalized = EvaluateCandidateCommand.model_validate(raw).model_dump(mode="json")
    assert normalized == canonical_command()
    await insert_refusal(database_session_factory, raw_override=raw, command_override=normalized)


@pytest.mark.asyncio
@pytest.mark.parametrize("token", ["2.0", "2e0", '"2"', "true"])
async def test_0072_raw_wait_version_remains_a_strict_json_integer(
    database_session_factory, token
) -> None:
    normalized = {**canonical_command(), "source_kind": "human_route", "expected_wait_version": 2}
    encoded = json.dumps(normalized, ensure_ascii=False)
    raw = encoded.replace('"expected_wait_version": 2', '"expected_wait_version": ' + token)
    with pytest.raises(DBAPIError, match="原始正文与规范命令不一致"):
        await insert_refusal(
            database_session_factory, raw_override=raw, command_override=normalized
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE investigation_candidate_evaluation_commands SET error_message='篡改'",
        "DELETE FROM investigation_candidate_evaluation_commands",
    ],
)
async def test_0072_refusal_audit_is_immutable(database_session_factory, mutation) -> None:
    await insert_refusal(database_session_factory)
    with pytest.raises(DBAPIError, match="不可更新或删除"):
        async with database_session_factory() as session, session.begin():
            await session.execute(text(mutation))


@pytest.mark.asyncio
async def test_0072_downgrade_preserves_candidate_facts(database_session_factory) -> None:
    await insert_refusal(database_session_factory)
    with pytest.raises(Exception, match="不能删除已保存的候选评估事实"):
        await asyncio.to_thread(command.downgrade, config(), "0071_human_reassessment_routing")
    async with database_session_factory() as session:
        assert await session.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0098_live_human_continuation"
        )
        assert (
            await session.scalar(
                text("SELECT count(*) FROM investigation_candidate_evaluation_commands")
            )
            == 1
        )


@pytest.mark.asyncio
async def test_0072_attempt_cannot_start_as_success(database_session_factory) -> None:
    with pytest.raises(DBAPIError, match="必须从运行状态开始"):
        async with database_session_factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO investigation_candidate_evaluation_attempts "
                    "(id,job_id,investigation_run_id,number,lease_token,worker_id,status,"
                    "started_at,lease_expires_at,finished_at) "
                    "VALUES (:id,:job,:run,1,:token,'migration-worker','succeeded',:now,:end,:now)"
                ),
                {
                    "id": uuid4(),
                    "job": uuid4(),
                    "run": uuid4(),
                    "token": uuid4(),
                    "now": BASE_TIME,
                    "end": BASE_TIME + timedelta(seconds=30),
                },
            )


@pytest.mark.asyncio
async def test_0072_ledger_rejects_forged_candidate_ownership(database_session_factory) -> None:
    store, started = await start_v3_run(database_session_factory, experience=False)
    claimed = await claim_v3(store)
    owner = uuid4()
    with pytest.raises(DBAPIError, match="真实执行归属"):
        async with database_session_factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO investigation_call_reservations (id,investigation_run_id,"
                    "investigation_attempt_id,candidate_attempt_id,sequence,idempotency_key,"
                    "operation,kind,status,reserved_at) VALUES (:id,:run,:parent,:owner,1,:key,"
                    "'evaluate_candidate','model','reserved',:now)"
                ),
                {
                    "id": uuid4(),
                    "run": started.run.id,
                    "parent": claimed.attempt.id,
                    "owner": owner,
                    "key": f"candidate:{owner}",
                    "now": BASE_TIME + timedelta(seconds=5),
                },
            )


@pytest.mark.asyncio
async def test_0072_job_requires_real_terminal_source(database_session_factory) -> None:
    _, started = await start_v3_run(database_session_factory, experience=False)
    with pytest.raises(DBAPIError, match="同运行最高终端"):
        async with database_session_factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO investigation_candidate_evaluation_jobs (id,investigation_run_id,"
                    "source_id,source_kind,source,source_content_sha256,packet,packet_sha256,"
                    "policy_version,source_sha256,requested_by_id,authorization_id,idempotency_key,"
                    "command_sha256,status,attempt_count,available_at,created_at,updated_at) "
                    "VALUES (:id,:run,:source,'terminal_handoff','{}'::jsonb,:hash,'{}'::jsonb,"
                    ":hash,'candidate-evaluation-v1',:hash,'migration-engineer',:authorization,"
                    "'migration-candidate',:hash,'queued',0,:now,:now,:now)"
                ),
                {
                    "id": uuid4(),
                    "run": started.run.id,
                    "source": uuid4(),
                    "authorization": uuid4(),
                    "hash": "a" * 64,
                    "now": BASE_TIME + timedelta(seconds=5),
                },
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", [None, "unreferenced_change", "risk", "approval", "threshold"])
async def test_0072_native_step_requires_cited_safe_parameters(
    database_session_factory, mutation
) -> None:
    source = {
        "service_name": "order-service",
        "review": {"accepted_supporting_evidence_ids": ["log-support", "change-release"]},
        "evidence": [
            {"evidence_id": "log-support", "kind": "log", "attributes": {}},
            {
                "evidence_id": "change-release",
                "kind": "change",
                "observed_at": BASE_TIME.isoformat(),
                "attributes": {
                    "change_type": "deployment",
                    "completed": True,
                    "from_version": "1.0",
                    "to_version": "1.1",
                },
            },
        ],
    }
    content = {
        "proposal": {
            "status": "candidate",
            "action_type": "rollback_release",
            "rationale": "根据当前已接受证据提出待审候选",
            "evidence_ids": ["log-support", "change-release"],
            "preconditions": ["后续规划必须重新确认运行版本"],
            "unresolved_questions": [],
        },
        "disposition": "planning_review",
        "step": {
            "step_order": 1,
            "action_type": "rollback_release",
            "target_type": "service",
            "target_name": "order-service",
            "parameters": {"current_version": "1.1", "target_version": "1.0"},
            "risk_level": "high",
            "requires_human_approval": True,
            "risk_rule_code": "rollback_changes_running_version",
            "risk_rationale": "回滚会把运行版本从 1.1 改为 1.0，必须由工程师审批。",
            "verification": {
                "signal": "http_5xx_ratio",
                "operator": "less_than",
                "threshold": 0.01,
                "observation_window_seconds": 60,
                "required_successful_samples": 3,
            },
        },
    }
    if mutation == "unreferenced_change":
        content["proposal"]["evidence_ids"] = ["log-support"]
    elif mutation == "risk":
        content["step"]["risk_level"] = "low"
    elif mutation == "approval":
        content["step"]["requires_human_approval"] = False
    elif mutation == "threshold":
        content["step"]["verification"]["threshold"] = 1

    async def check() -> None:
        async with database_session_factory() as session, session.begin():
            await session.execute(
                text(
                    "SELECT assert_candidate_step(CAST(:content AS jsonb),CAST(:source AS jsonb))"
                ),
                {"content": json.dumps(content), "source": json.dumps(source)},
            )

    if mutation is None:
        await check()
    else:
        with pytest.raises(DBAPIError, match="候选步骤"):
            await check()
