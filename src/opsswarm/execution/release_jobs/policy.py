"""原固定窗口、派发资格和结果归类的确定性规则。"""

from datetime import datetime
from typing import Literal
from uuid import UUID, uuid5

from opsswarm.execution.release.contracts import ReleaseRequestRecord
from opsswarm.execution.release_jobs.client import DispatchObservation, validate_observation
from opsswarm.execution.release_jobs.contracts import (
    ExecutionJobError,
    ExecutionJobRecord,
    ExecutionResultContent,
)
from opsswarm.fault_lab.release.dispatch_contracts import DispatchCommand
from opsswarm.investigation.context.hashing import content_hash


def execution_id(request_id: UUID) -> UUID:
    return uuid5(request_id, "candidate-release-execution-job:v1")


def dispatch_command(
    request: ReleaseRequestRecord, *, created_at: datetime, credential_expires_at: datetime
) -> DispatchCommand:
    return DispatchCommand(
        operation_id=request.operation_id,
        command=request.content.cas_command,
        not_before=created_at,
        expires_at=min(request.expires_at, credential_expires_at),
    )


def require_window(job: ExecutionJobRecord, now: datetime, lease_expires_at: datetime) -> None:
    if not job.dispatch_command.not_before <= now < job.dispatch_command.expires_at:
        raise ExecutionJobError("release_execution_expired", "原固定派发窗口已失效")
    if lease_expires_at < job.dispatch_command.expires_at or now >= lease_expires_at:
        raise ExecutionJobError("release_execution_lease_lost", "有效租约未覆盖固定派发窗口")


def result_content(
    job: ExecutionJobRecord,
    observation: DispatchObservation | None,
    *,
    now: datetime,
    reason_code: str,
) -> ExecutionResultContent:
    outcome: Literal[
        "applied_current", "applied_superseded", "not_applied", "unknown", "blocked"
    ] = "blocked" if job.phase == "prepared" else "unknown"
    checked = None
    if observation is not None:
        checked = validate_observation(job.dispatch_command, observation, now=now)
        if job.phase != "dispatched":
            raise ExecutionJobError(
                "release_execution_result_invalid", "未派发任务不能携带远端执行观察"
            )
        view = checked.view
        if view.record.status == "applied":
            outcome = "applied_current" if view.receipt.effect_current else "applied_superseded"
        elif view.availability in {"expired", "cancelled"}:
            outcome = "not_applied"
    return ExecutionResultContent(
        request_id=job.request_id,
        request_content_sha256=job.request_content_sha256,
        operation_id=job.operation_id,
        dispatch_sha256=content_hash(job.dispatch_command),
        outcome=outcome,
        observation=checked,
        reason_code=reason_code,
        created_at=now,
    )


def terminal_status(content: ExecutionResultContent) -> str:
    if content.outcome.startswith("applied_"):
        return "succeeded"
    return "unknown" if content.outcome == "unknown" else "failed"
