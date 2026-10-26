"""发布执行准备的可信主体、三资源与完整原始正文绑定。"""

import hashlib
from datetime import datetime
from uuid import UUID

from opsswarm.execution.release.contracts import ReleaseRequestCommand, ReleaseRequestError
from opsswarm.identity.tables import OperatorAuthorizationRow

OPERATION = "prepare_candidate_release_execution"
ROUTE = (
    "/api/v1/investigations/{run_id}/candidate-evaluations/{job_id}"
    "/execution-conditions/versions/{version}/release-execution-request"
)


def validate_admission(
    admission: OperatorAuthorizationRow | None,
    *,
    run_id: UUID,
    job_id: UUID,
    version: int,
    subject_id: str,
    command: ReleaseRequestCommand,
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
            or admission.resources
            != dict(run_id=str(run_id), job_id=str(job_id), version=str(version))
            or "execute_action" not in admission.permissions
            or admission.authorized_at > occurred_at
            or not 2 <= len(raw) <= 65536
            or admission.body_sha256 != hashlib.sha256(raw).hexdigest()
            or ReleaseRequestCommand.model_validate_json(raw) != command
        ):
            raise ValueError("发布执行准备命令与准入不一致")
    except (ValueError, UnicodeError) as error:
        raise ReleaseRequestError(
            "candidate_release_integrity_violation", "发布执行准备缺少同源可信准入"
        ) from error
