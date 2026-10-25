"""真实 PostgreSQL 验证候选草案的来源隔离、可信命令和不可变闭环。"""

import asyncio
import copy
import hashlib
import json
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4, uuid5

import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from alembic import command
from opsswarm.investigation.candidate.contracts import CandidateSource
from opsswarm.investigation.candidate.output import (
    CandidateEvaluation,
    CandidateModelResult,
    CandidateProposal,
)
from opsswarm.investigation.candidate.tables import CandidateJobRow, CandidateResultRow
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.planning.candidate.contracts import PlanCommand
from opsswarm.planning.candidate.policy import build_plan
from tests.api.test_candidate_evaluation_api import context, evaluate, worker
from tests.api.test_human_reassessment_api import RecordingModel
from tests.api.test_human_wait_api import wait_context as wait_context
from tests.task_facts_helpers import BASE_TIME

pytestmark = pytest.mark.integration

ROUTE = "/api/v1/investigations/{run_id}/candidate-evaluations/{job_id}/action-plan"


def config() -> Config:
    return Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))


def encoded(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def canonical_command() -> dict:
    return {
        "action": "create_plan",
        "candidate_result_sha256": "a" * 64,
        "expected_wait_version": None,
        "expected_incident_version": 2,
        "idempotency_key": "migration-candidate-plan",
        "reason": "工程师请求生成尚未授权的独立草案",
    }


async def authorize(session, values, *, invalid=None) -> None:
    permissions = ["plan_action", "manage_incident"]
    if invalid in permissions:
        permissions.remove(invalid)
    await session.execute(
        text(
            "INSERT INTO operator_authorizations (id,policy_version,subject_id,credential_id,"
            "role,permissions,operation,method,route,resources,body_sha256,authorized_at) "
            "VALUES (:authorization,'operator-auth-v1','migration-engineer','migration',:role,"
            "CAST(:permissions AS jsonb),:operation,'POST',:route,CAST(:resources AS jsonb),"
            ":body_hash,:now)"
        ),
        {
            **values,
            "role": "viewer" if invalid == "role" else "engineer",
            "permissions": encoded(permissions),
            "operation": "evaluate_investigation_candidate"
            if invalid == "operation"
            else "create_candidate_action_plan",
            "route": ROUTE + "/commands" if invalid == "route" else ROUTE,
            "resources": encoded(
                {
                    "run_id": str(uuid4() if invalid == "run" else values["run"]),
                    "job_id": str(uuid4() if invalid == "job" else values["job"]),
                }
            ),
            "body_hash": "b" * 64 if invalid == "body_hash" else digest(values["raw"]),
        },
    )


async def insert_audit(session, values, *, outcome="error") -> None:
    await session.execute(
        text(
            "INSERT INTO candidate_plan_commands (authorization_id,investigation_run_id,"
            "candidate_job_id,subject_id,command,raw_body,command_sha256,outcome,http_status,"
            "plan_id,error_code,error_message,created_at) VALUES (:authorization,:run,:job,"
            ":subject,CAST(:command AS jsonb),:raw,:command_hash,:outcome,:status,:plan,"
            ":error,:message,:now)"
        ),
        {
            **values,
            "outcome": outcome,
            "status": {"error": 404, "applied": 201, "replayed": 200}[outcome],
            "plan": None if outcome == "error" else values["id"],
            "error": "candidate_plan_run_not_found" if outcome == "error" else None,
            "message": "目标运行不存在" if outcome == "error" else None,
        },
    )


async def insert_refusal(factory, *, invalid=None, raw=None, canonical=None) -> None:
    body = canonical_command() if canonical is None else canonical
    raw = encoded(body) if raw is None else raw if isinstance(raw, str) else encoded(raw)
    values = {
        "authorization": uuid4(),
        "run": uuid4(),
        "job": uuid4(),
        "subject": "another-engineer" if invalid == "subject" else "migration-engineer",
        "command": encoded(body),
        "command_hash": "c" * 64 if invalid == "command_hash" else digest(encoded(body)),
        "raw": raw,
        "now": BASE_TIME,
    }
    async with factory() as session, session.begin():
        await authorize(session, values, invalid=invalid)
        await insert_audit(session, values)


@pytest.mark.asyncio
async def test_0073_metadata_and_empty_roundtrip(database_session_factory) -> None:
    await asyncio.to_thread(command.check, config())
    try:
        await asyncio.to_thread(command.downgrade, config(), "0072_candidate_evaluation")
        async with database_session_factory() as session:
            assert await session.scalar(text("SELECT version_num FROM alembic_version")) == (
                "0072_candidate_evaluation"
            )
            for table in ("candidate_action_plans", "candidate_plan_commands"):
                assert (
                    await session.scalar(text("SELECT to_regclass(:name)"), {"name": table}) is None
                )
        await asyncio.to_thread(command.upgrade, config(), "head")
        await asyncio.to_thread(command.check, config())
    finally:
        await asyncio.to_thread(command.upgrade, config(), "head")


@pytest.mark.asyncio
async def test_0073_guards_cover_plan_and_command_without_legacy_columns(
    database_session_factory,
) -> None:
    async with database_session_factory() as session:
        guards = dict(
            (
                await session.execute(
                    text(
                        "SELECT tgname,tgdeferrable FROM pg_trigger WHERE "
                        "tgname IN ('candidate_action_plans_guard','candidate_plan_commands_guard',"
                        "'candidate_action_plans_state','candidate_plan_commands_state')"
                    )
                )
            ).all()
        )
        assert guards == {
            "candidate_action_plans_guard": False,
            "candidate_plan_commands_guard": False,
            "candidate_action_plans_state": True,
            "candidate_plan_commands_state": True,
        }
        assert (
            await session.scalar(
                text(
                    "SELECT count(*) FROM information_schema.columns WHERE table_name IN "
                    "('action_plans','approval_requests','operation_executions') "
                    "AND column_name LIKE '%candidate%'"
                )
            )
            == 0
        )
        constraints = dict(
            (
                await session.execute(
                    text(
                        "SELECT conname,pg_get_constraintdef(oid) FROM pg_constraint "
                        "WHERE conname IN ('fk_candidate_plans_job_run','uq_candidate_plans_job',"
                        "'uq_candidate_plans_key')"
                    )
                )
            ).all()
        )
        assert "candidate_job_id, investigation_run_id" in constraints["fk_candidate_plans_job_run"]
        assert constraints["uq_candidate_plans_job"] == "UNIQUE (candidate_job_id)"
        assert (
            "investigation_run_id, requested_by_id, idempotency_key"
            in (constraints["uq_candidate_plans_key"])
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid",
    [
        "role",
        "operation",
        "route",
        "run",
        "job",
        "body_hash",
        "subject",
        "plan_action",
        "manage_incident",
    ],
)
async def test_0073_refusal_requires_precise_trusted_admission(
    database_session_factory, invalid
) -> None:
    with pytest.raises(DBAPIError, match="精确原始正文或同源可信准入"):
        await insert_refusal(database_session_factory, invalid=invalid)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field", ["reason", "idempotency_key", "candidate_result_sha256", "expected_incident_version"]
)
async def test_0073_raw_body_cannot_authorize_a_different_canonical_command(
    database_session_factory, field
) -> None:
    original = canonical_command()
    altered = {
        **original,
        field: {
            "reason": "另一份合法但未经此次正文授权的理由",
            "idempotency_key": "another-valid-plan-key",
            "candidate_result_sha256": "b" * 64,
            "expected_incident_version": 3,
        }[field],
    }
    PlanCommand.model_validate(original)
    PlanCommand.model_validate(altered)
    with pytest.raises(DBAPIError, match="原始正文与规范命令不一致"):
        await insert_refusal(database_session_factory, raw=original, canonical=altered)


@pytest.mark.asyncio
@pytest.mark.parametrize("normalization", ["default_action", "ascii_trim", "unicode_trim"])
async def test_0073_raw_body_accepts_actual_pydantic_normalization(
    database_session_factory, normalization
) -> None:
    raw = canonical_command()
    if normalization == "default_action":
        raw.pop("action")
    else:
        padding = " \t\r\n" if normalization == "ascii_trim" else "\u3000\u00a0\u2003"
        for field in ("reason", "idempotency_key", "candidate_result_sha256"):
            raw[field] = padding + raw[field] + padding
    normalized = PlanCommand.model_validate(raw).model_dump(mode="json")
    assert normalized == canonical_command()
    await insert_refusal(database_session_factory, raw=raw, canonical=normalized)


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["expected_wait_version", "expected_incident_version"])
@pytest.mark.parametrize("token", ["2.0", "2e0", '"2"', "true"])
async def test_0073_raw_versions_are_strict_json_integers(
    database_session_factory, field, token
) -> None:
    canonical = {**canonical_command(), "expected_wait_version": 2}
    raw = encoded(canonical).replace('"' + field + '":2', '"' + field + '":' + token)
    with pytest.raises(DBAPIError, match="原始正文与规范命令不一致"):
        await insert_refusal(database_session_factory, raw=raw, canonical=canonical)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"action": "execute"},
        {"candidate_result_sha256": "invalid"},
        {"expected_wait_version": 1},
        {"expected_incident_version": 0},
        {"execution_eligible": True},
        {"reason": "\u3000"},
    ],
)
async def test_0073_native_command_rejects_invalid_contract(
    database_session_factory, change
) -> None:
    with pytest.raises(DBAPIError):
        await insert_refusal(database_session_factory, canonical={**canonical_command(), **change})


@pytest.mark.asyncio
async def test_0073_native_command_rejects_a_forged_canonical_hash(
    database_session_factory,
) -> None:
    with pytest.raises(DBAPIError, match="有界精确规范来源绑定"):
        await insert_refusal(database_session_factory, invalid="command_hash")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE candidate_plan_commands SET error_message='篡改'",
        "DELETE FROM candidate_plan_commands",
    ],
)
async def test_0073_refusal_is_immutable(database_session_factory, mutation) -> None:
    await insert_refusal(database_session_factory)
    with pytest.raises(DBAPIError, match="不可更新或删除"):
        async with database_session_factory() as session, session.begin():
            await session.execute(text(mutation))


@pytest.mark.asyncio
async def test_0073_downgrade_preserves_even_refusal_facts(database_session_factory) -> None:
    await insert_refusal(database_session_factory)
    with pytest.raises(Exception, match="不能删除已保存的候选规划事实"):
        await asyncio.to_thread(command.downgrade, config(), "0072_candidate_evaluation")
    async with database_session_factory() as session:
        assert await session.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0077_candidate_conditions"
        )
        assert await session.scalar(text("SELECT count(*) FROM candidate_plan_commands")) == 1


async def ready_plan(c) -> dict:
    """建立真实候选成功链，原生计划写入不借助待验证的应用服务。"""
    await context(c)
    response = await evaluate(c)
    assert response.status_code == 201, response.text
    job_id = UUID(response.json()["job"]["id"])
    async with c.db() as session:
        job = await session.get(CandidateJobRow, job_id)
        source = CandidateSource.model_validate(job.source)
    model = RecordingModel(
        result=CandidateModelResult(
            proposal=CandidateProposal(
                status="candidate",
                action_type="rollback_release",
                rationale="核对明确版本的回滚草案",
                evidence_ids=tuple(e.evidence_id for e in source.evidence),
                preconditions=("执行前另行可信审批",),
                unresolved_questions=("现场状态须在后续阶段重新核验",),
            )
        )
    )
    assert await worker(c, model).run_once()
    async with c.db() as session:
        result = await session.get(CandidateResultRow, job_id)
        incident_version = await session.scalar(
            text("SELECT version FROM incidents WHERE id=:id"), {"id": c.started.run.incident_id}
        )
    content = build_plan(
        job_id, source, CandidateEvaluation.model_validate(result.content), result.content_sha256
    )
    body = PlanCommand(
        candidate_result_sha256=result.content_sha256,
        expected_wait_version=job.wait_version,
        expected_incident_version=incident_version,
        idempotency_key="native-candidate-plan",
        reason="验证同事务成功命令闭环与原生守卫",
    )
    return {
        "id": uuid5(job_id, "candidate-action-plan:v1"),
        "run": c.started.run.id,
        "job": job_id,
        "result_hash": result.content_sha256,
        "wait": job.wait_version,
        "incident": incident_version,
        "subject": "migration-engineer",
        "authorization": uuid4(),
        "key": body.idempotency_key,
        "command": encoded(body.model_dump(mode="json")),
        "command_hash": content_hash(body),
        "raw": encoded(body.model_dump(mode="json")),
        "content": encoded(content.model_dump(mode="json")),
        "content_hash": content_hash(content),
        "now": c.clock.current,
    }


async def insert_plan(session, values) -> None:
    await session.execute(
        text(
            "INSERT INTO candidate_action_plans (id,investigation_run_id,candidate_job_id,"
            "candidate_result_sha256,wait_version,incident_version,policy_version,"
            "requested_by_id,authorization_id,idempotency_key,command_sha256,content,"
            "content_sha256,created_at) VALUES (:id,:run,:job,:result_hash,:wait,:incident,"
            "'candidate-plan-v1',:subject,:authorization,:key,:command_hash,"
            "CAST(:content AS jsonb),"
            ":content_hash,:now)"
        ),
        values,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_0073_native_plan_success_is_isolated_and_immutable(wait_context) -> None:
    c = wait_context
    values = await ready_plan(c)
    async with c.db() as session, session.begin():
        await authorize(session, values)
        await insert_plan(session, values)
        await insert_audit(session, values, outcome="applied")
    async with c.db() as session:
        assert await session.scalar(text("SELECT count(*) FROM candidate_action_plans")) == 1
        for table in ("action_plans", "approval_requests", "operation_executions"):
            assert await session.scalar(text(f"SELECT count(*) FROM {table}")) == 0
    for mutation in (
        "UPDATE candidate_action_plans SET content='{}'::jsonb",
        "DELETE FROM candidate_action_plans",
    ):
        with pytest.raises(DBAPIError, match="不可更新或删除"):
            async with c.db() as session, session.begin():
                await session.execute(text(mutation))
    with pytest.raises(Exception, match="不能删除已保存的候选规划事实"):
        await asyncio.to_thread(command.downgrade, config(), "0072_candidate_evaluation")


@pytest.mark.asyncio
@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_0073_native_plan_without_applied_audit_cannot_commit(wait_context) -> None:
    c = wait_context
    values = await ready_plan(c)
    with pytest.raises(DBAPIError, match="同事务精确成功命令闭环"):
        async with c.db() as session, session.begin():
            await authorize(session, values)
            await insert_plan(session, values)
    async with c.db() as session:
        assert await session.scalar(text("SELECT count(*) FROM candidate_action_plans")) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
@pytest.mark.parametrize(
    "mutation", ["result_hash", "incident", "wait", "deadline", "risk", "auth"]
)
async def test_0073_native_plan_cannot_bypass_source_or_authorization(
    wait_context, mutation
) -> None:
    c = wait_context
    original = await ready_plan(c)
    values = copy.deepcopy(original)
    if mutation == "result_hash":
        values["result_hash"] = "b" * 64
    elif mutation == "incident":
        values["incident"] += 1
    elif mutation == "wait":
        values["wait"] = 2
    elif mutation == "deadline":
        values["now"] += timedelta(seconds=901)
    elif mutation == "risk":
        content = json.loads(values["content"])
        content["plan"]["risk_level"] = "low"
        values["content"] = encoded(content)
        values["content_hash"] = digest(values["content"])
    with pytest.raises(DBAPIError):
        async with c.db() as session, session.begin():
            await authorize(
                session, original, invalid="manage_incident" if mutation == "auth" else None
            )
            await insert_plan(session, values)
            await insert_audit(session, values, outcome="applied")
