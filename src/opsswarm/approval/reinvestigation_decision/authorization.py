"""把候选审批决定命令绑定到原始 HTTP 正文与可信审批者准入。"""

import hashlib
from datetime import datetime
from uuid import UUID

from opsswarm.approval.reinvestigation_decision.contracts import (
    ReflowDecisionCommand,
    ReflowDecisionError,
)
from opsswarm.identity.models import OperatorAccessError, OperatorPrincipal
from opsswarm.identity.tables import OperatorAuthorizationRow

OPERATION = "decide_reinvestigation_approval"
ROUTE = (
    "/api/v1/investigations/{run_id}/reinvestigation-candidate/action-plan"
    "/approval-request/decision"
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
    command: ReflowDecisionCommand,
    raw_body: str,
    occurred_at: datetime,
) -> None:
    try:
        raw = raw_body.encode("utf-8")
        if (
            admission is None
            or admission.policy_version != "operator-auth-v1"
            or admission.subject_id != subject_id
            or admission.role != "approver"
            or admission.method != "POST"
            or admission.operation != OPERATION
            or admission.route != ROUTE
            or job_id != run_id
            or admission.resources != {"run_id": str(run_id)}
            or "decide_approval" not in admission.permissions
            or admission.authorized_at > occurred_at
            or not 2 <= len(raw) <= 65536
            or hashlib.sha256(raw).hexdigest() != admission.body_sha256
            or ReflowDecisionCommand.model_validate_json(raw) != command
        ):
            raise ValueError("候选审批决定命令与可信准入不一致")
    except (ValueError, UnicodeError) as error:
        raise ReflowDecisionError(
            "reflow_decision_integrity_violation", "候选审批决定命令缺少同源可信授权"
        ) from error
