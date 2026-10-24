"""把候选审批请求命令绑定到原始 HTTP 正文与可信工程师准入。"""

import hashlib
from datetime import datetime
from uuid import UUID

from opsswarm.approval.candidate.contracts import RequestCommand, RequestError
from opsswarm.identity.models import OperatorAccessError, OperatorPrincipal
from opsswarm.identity.tables import OperatorAuthorizationRow

OPERATION = "create_candidate_approval_request"
ROUTE = (
    "/api/v1/investigations/{run_id}/candidate-evaluations/{job_id}/action-plan/approval-request"
)


def check_principal(principal: OperatorPrincipal, now: datetime) -> None:
    if now >= principal.expires_at:
        raise OperatorAccessError("operator_unauthenticated", "操作者认证已经过期")


def validate_admission(
    admission: OperatorAuthorizationRow | None,
    *,
    run_id: UUID,
    job_id: UUID,
    subject_id: str,
    command: RequestCommand,
    raw_body: str,
    occurred_at: datetime,
) -> None:
    try:
        raw = raw_body.encode("utf-8")
        if (
            admission is None
            or admission.policy_version != "operator-auth-v1"
            or admission.subject_id != subject_id
            or admission.role != "engineer"
            or admission.method != "POST"
            or admission.operation != OPERATION
            or admission.route != ROUTE
            or admission.resources != {"run_id": str(run_id), "job_id": str(job_id)}
            or "manage_incident" not in admission.permissions
            or "request_approval" not in admission.permissions
            or admission.authorized_at > occurred_at
            or not 2 <= len(raw) <= 65536
            or hashlib.sha256(raw).hexdigest() != admission.body_sha256
            or RequestCommand.model_validate_json(raw) != command
        ):
            raise ValueError("候选审批请求命令与可信准入不一致")
    except (ValueError, UnicodeError) as error:
        raise RequestError(
            "candidate_approval_integrity_violation", "候选审批请求命令缺少同源可信授权"
        ) from error
