"""执行前检查命令与真实操作者准入的精确绑定。"""

import hashlib
from datetime import datetime
from uuid import UUID

from opsswarm.execution.candidate.contracts import PreflightCommand, PreflightError
from opsswarm.identity.tables import OperatorAuthorizationRow

OPERATION = "check_candidate_execution"
ROUTE = "/api/v1/investigations/{run_id}/candidate-evaluations/{job_id}/execution-preflights"


def validate_admission(
    admission: OperatorAuthorizationRow | None,
    *,
    run_id: UUID,
    job_id: UUID,
    subject_id: str,
    command: PreflightCommand,
    raw_body: str,
    occurred_at: datetime,
) -> None:
    try:
        raw = raw_body.encode("utf-8")
        if (
            admission is None
            or admission.policy_version != "operator-auth-v1"
            or admission.subject_id != subject_id
            or admission.role != "executor"
            or admission.method != "POST"
            or admission.operation != OPERATION
            or admission.route != ROUTE
            or admission.resources != {"run_id": str(run_id), "job_id": str(job_id)}
            or "execute_action" not in admission.permissions
            or admission.authorized_at > occurred_at
            or not 2 <= len(raw) <= 65536
            or hashlib.sha256(raw).hexdigest() != admission.body_sha256
            or PreflightCommand.model_validate_json(raw) != command
        ):
            raise ValueError("执行前检查命令与可信准入不一致")
    except (ValueError, UnicodeError) as error:
        raise PreflightError(
            "candidate_preflight_integrity_violation", "执行前检查缺少同源可信授权"
        ) from error
