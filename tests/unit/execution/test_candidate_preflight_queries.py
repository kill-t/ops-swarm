"""检查历史查询将损坏来源转换为稳定公开冲突，不泄漏内部异常。"""

from datetime import timedelta
from unittest.mock import AsyncMock
from uuid import uuid4, uuid5

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from opsswarm.approval.decision.contracts import DecisionError
from opsswarm.execution.candidate import queries
from opsswarm.execution.candidate.contracts import POLICY_VERSION, PreflightCommand, PreflightError
from opsswarm.execution.candidate.policy import build_content
from opsswarm.execution.candidate.tables import CandidatePreflightCommandRow, CandidatePreflightRow
from opsswarm.investigation.budget.policy import CallBudgetExceeded
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.tasks.errors import TaskFactConflict
from tests.unit.execution.test_candidate_preflight import (
    NOW,
    command_data,
    decision_for,
    observation_for,
    step_for,
)


def invalid_command_error():
    try:
        PreflightCommand.model_validate(command_data(expected_incident_version=True))
    except ValidationError as error:
        return error
    raise AssertionError("损坏命令应违反严格事故版本契约")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        TaskFactConflict("handoff_integrity_violation", "内部来源损坏 secret-detail"),
        CallBudgetExceeded("model_budget_invalid"),
        invalid_command_error(),
    ],
    ids=["handoff-integrity", "budget-integrity", "structured-validation"],
)
async def test_load_context_reports_corrupt_source_as_public_conflict(monkeypatch, failure):
    monkeypatch.setattr(queries, "_load_context", AsyncMock(side_effect=failure))
    with pytest.raises(PreflightError) as caught:
        await queries.load_context(
            AsyncMock(spec=AsyncSession), object(), uuid4(), clock=lambda: NOW
        )
    assert caught.value.status_code == 409
    assert caught.value.code == "candidate_preflight_source_invalid"
    assert caught.value.__cause__ is failure
    assert "secret-detail" not in str(caught.value)


@pytest.mark.asyncio
async def test_load_context_preserves_source_not_found_status(monkeypatch):
    failure = DecisionError("candidate_decision_request_not_found", "原审批不存在", 404)
    monkeypatch.setattr(queries, "_load_context", AsyncMock(side_effect=failure))
    with pytest.raises(PreflightError) as caught:
        await queries.load_context(
            AsyncMock(spec=AsyncSession), object(), uuid4(), clock=lambda: NOW
        )
    assert caught.value.status_code == 404
    assert caught.value.code == "candidate_preflight_source_invalid"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["load_context", "read_record", "validated_audit"])
async def test_existing_preflight_error_keeps_its_precise_identity(monkeypatch, operation):
    failure = PreflightError("candidate_preflight_decision_not_found", "候选尚无可信决定", 404)
    monkeypatch.setattr(queries, "_" + operation, AsyncMock(side_effect=failure))
    session = AsyncMock(spec=AsyncSession)
    with pytest.raises(PreflightError) as caught:
        if operation == "load_context":
            await queries.load_context(session, object(), uuid4(), clock=lambda: NOW)
        elif operation == "read_record":
            await queries.read_record(session, object(), object())
        else:
            await queries.validated_audit(session, object())
    assert caught.value is failure


def valid_fact_values():
    decision = decision_for()
    content = build_content(decision, step_for(), observation_for())
    operation_id = uuid5(decision.id, "candidate-operation:v1")
    command = PreflightCommand.model_validate(
        command_data(
            decision_id=decision.id,
            decision_content_sha256=decision.content_sha256,
            expected_incident_version=decision.incident_version,
        )
    )
    return {
        "cursor": 1,
        "id": queries.record_id(operation_id, "executor-1", command.idempotency_key),
        "operation_id": operation_id,
        "investigation_run_id": decision.investigation_run_id,
        "candidate_job_id": decision.candidate_job_id,
        "decision_id": decision.id,
        "decision_content_sha256": decision.content_sha256,
        "incident_version": decision.incident_version,
        "policy_version": POLICY_VERSION,
        "requested_by_id": "executor-1",
        "authorization_id": uuid4(),
        "idempotency_key": command.idempotency_key,
        "command_sha256": content_hash(command),
        "content": content.model_dump(mode="json"),
        "content_sha256": content_hash(content),
        "created_at": NOW,
        "expires_at": NOW + timedelta(seconds=30),
    }


@pytest.mark.asyncio
async def test_malformed_persisted_fact_is_a_conflict_instead_of_validation_error():
    row = CandidatePreflightRow(**{**valid_fact_values(), "cursor": 0})
    with pytest.raises(PreflightError) as caught:
        await queries.read_record(AsyncMock(spec=AsyncSession), row, object())
    assert caught.value.status_code == 409
    assert caught.value.code == "candidate_preflight_integrity_violation"
    assert isinstance(caught.value.__cause__, ValidationError)
    assert caught.value.__cause__.errors()[0]["loc"] == ("cursor",)


@pytest.mark.asyncio
async def test_persisted_audit_with_incomplete_command_is_a_public_integrity_conflict():
    row = CandidatePreflightCommandRow(
        cursor=1,
        authorization_id=uuid4(),
        investigation_run_id=uuid4(),
        candidate_job_id=uuid4(),
        subject_id="executor-1",
        command={},
        raw_body="{}",
        command_sha256="a" * 64,
        outcome="error",
        http_status=409,
        preflight_id=None,
        error_code="candidate_preflight_not_current",
        error_message="批准当前失效",
        created_at=NOW,
    )
    with pytest.raises(PreflightError) as caught:
        await queries.validated_audit(AsyncMock(spec=AsyncSession), row)
    assert caught.value.status_code == 409
    assert caught.value.code == "candidate_preflight_integrity_violation"
    assert isinstance(caught.value.__cause__, ValidationError)
