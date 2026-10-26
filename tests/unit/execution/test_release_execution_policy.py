"""执行委派轮换、固定窗口及未知结果的确定性边界。"""

from datetime import timedelta
from uuid import UUID

import pytest
from pydantic import ValidationError

from opsswarm.execution.release_jobs.authorization import (
    binding,
    credential_for,
    principal_credential,
)
from opsswarm.execution.release_jobs.client import DispatchClientError, DispatchObservation
from opsswarm.execution.release_jobs.contracts import (
    ExecutionJobError,
    ExecutionJobRecord,
    ExecutionResultContent,
)
from opsswarm.execution.release_jobs.policy import (
    execution_id,
    require_window,
    result_content,
    terminal_status,
)
from opsswarm.identity.models import OperatorAccessError, OperatorPrincipal, OperatorRole
from opsswarm.identity.settings import OperatorAuthSettings, OperatorCredential
from opsswarm.investigation.context.hashing import content_hash
from tests.unit.execution.test_release_job_client import DISPATCH_PATH, NOW, command, view


def credential():
    return OperatorCredential(
        credential_id="executor",
        subject_id="trusted-executor",
        role=OperatorRole.EXECUTOR,
        secret_sha256="a" * 64,
        expires_at=NOW + timedelta(minutes=1),
    )


def settings(value=None):
    return OperatorAuthSettings(
        mode="bearer", credentials=(credential() if value is None else value,)
    )


@pytest.mark.parametrize(
    "change",
    [
        {"subject_id": "other-executor"},
        {"credential_id": "executor-two"},
        {"role": OperatorRole.ENGINEER},
        {"secret_sha256": "b" * 64},
        {"expires_at": NOW + timedelta(seconds=61)},
    ],
)
def test_same_id_or_rotated_config_does_not_reuse_execution_delegation(change):
    original = credential()
    updated = OperatorCredential.model_validate(
        {**original.model_dump(), "secret_sha256": original.secret_sha256, **change}
    )
    assert binding(updated) != binding(original)
    with pytest.raises(OperatorAccessError):
        credential_for(
            settings(updated),
            credential_id=original.credential_id,
            subject_id=original.subject_id,
            now=NOW,
            expected_binding=binding(original),
        )


def test_current_credential_is_not_replaced_by_historical_principal():
    original = credential()
    principal = OperatorPrincipal(
        subject_id=original.subject_id,
        credential_id=original.credential_id,
        role=original.role,
        expires_at=original.expires_at,
    )
    assert principal_credential(settings(), principal, NOW) == original
    with pytest.raises(OperatorAccessError):
        principal_credential(settings(), principal, original.expires_at)
    with pytest.raises(OperatorAccessError):
        credential_for(
            OperatorAuthSettings(),
            credential_id=original.credential_id,
            subject_id=original.subject_id,
            now=NOW,
        )


def job(phase="dispatched"):
    remote = command()
    return ExecutionJobRecord(
        id=execution_id(UUID(int=1)),
        investigation_run_id=UUID(int=2),
        candidate_job_id=UUID(int=3),
        verification_version=1,
        request_id=UUID(int=1),
        request_content_sha256="a" * 64,
        operation_id=remote.operation_id,
        policy_version="candidate-release-execution-v1",
        requested_by_id="executor",
        credential_id="executor",
        credential_binding_sha256="b" * 64,
        credential_expires_at=NOW + timedelta(seconds=60),
        authorization_id=UUID(int=4),
        idempotency_key="job-test-key",
        command_sha256="c" * 64,
        dispatch_command=remote,
        dispatch_sha256=content_hash(remote),
        status="running",
        phase=phase,
        attempt_count=1,
        locked_by="worker",
        lease_token=UUID(int=5),
        locked_until=NOW + timedelta(seconds=60),
        dispatched_at=NOW if phase == "dispatched" else None,
        dispatch_attempt_id=UUID(int=6) if phase == "dispatched" else None,
        created_at=remote.not_before,
        updated_at=NOW,
        completed_at=None,
        last_error_code=None,
    )


@pytest.mark.parametrize(
    "now,lease",
    [
        (NOW - timedelta(seconds=6), NOW + timedelta(seconds=60)),
        (NOW + timedelta(seconds=15), NOW + timedelta(seconds=60)),
        (NOW, NOW + timedelta(seconds=14)),
        (NOW, NOW),
    ],
)
def test_window_and_lease_cannot_be_extended(now, lease):
    with pytest.raises(ExecutionJobError):
        require_window(job(), now, lease)


@pytest.mark.parametrize(
    "phase,outcome,status",
    [
        ("prepared", "blocked", "failed"),
        ("dispatched", "unknown", "unknown"),
    ],
)
def test_no_material_after_dispatch_is_unknown_not_false_failure(phase, outcome, status):
    result = result_content(job(phase), None, now=NOW, reason_code="test_unavailable")
    assert result.outcome == outcome and terminal_status(result) == status
    assert result.parent_advanced is False


@pytest.mark.parametrize(
    "state,superseded,outcome",
    [
        ("applied", False, "applied_current"),
        ("applied", True, "applied_superseded"),
        ("prepared", False, "unknown"),
        ("cancelled", False, "not_applied"),
    ],
)
def test_result_preserves_remote_historical_meaning(state, superseded, outcome):
    observation = DispatchObservation(
        view=view(state, superseded=superseded), observed_at=NOW, source_path=DISPATCH_PATH
    )
    result = result_content(job(), observation, now=NOW, reason_code="test_read")
    assert result.outcome == outcome and result.observation == observation


def test_expired_remote_can_close_without_granting_retry_and_old_material_is_rejected():
    now = NOW + timedelta(seconds=20)
    observation = DispatchObservation(
        view=view(checked=now), observed_at=now, source_path=DISPATCH_PATH
    )
    assert (
        result_content(job(), observation, now=now, reason_code="test_expired").outcome
        == "not_applied"
    )
    with pytest.raises(DispatchClientError):
        result_content(
            job(), observation, now=now + timedelta(seconds=30), reason_code="test_stale"
        )
    with pytest.raises(ExecutionJobError):
        result_content(job("prepared"), observation, now=now, reason_code="test_unwritten")


@pytest.mark.parametrize("value", [True, 0, "false"])
def test_result_cannot_claim_parent_advanced_or_numeric_false(value):
    good = result_content(job(), None, now=NOW, reason_code="test_unknown")
    with pytest.raises(ValidationError):
        ExecutionResultContent.model_validate({**good.model_dump(), "parent_advanced": value})
