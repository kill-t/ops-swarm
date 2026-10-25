"""两阶段审批命令的原始正文、可信角色和三资源绑定。"""

import hashlib
from datetime import datetime
from typing import Literal
from uuid import UUID

from opsswarm.approval.conditions.contracts import ApprovalError, DecisionCommand, RequestCommand
from opsswarm.identity.models import OperatorRole
from opsswarm.identity.tables import OperatorAuthorizationRow

Stage = Literal["request", "decision"]
Command = RequestCommand | DecisionCommand
BASE_ROUTE = (
    "/api/v1/investigations/{run_id}/candidate-evaluations/{job_id}"
    "/execution-conditions/versions/{version}/approval-request"
)


def command_stage(command: Command) -> Stage:
    return "request" if isinstance(command, RequestCommand) else "decision"


def operation(stage: Stage) -> str:
    return (
        "create_condition_approval_request" if stage == "request" else "decide_condition_approval"
    )


def role(stage: Stage) -> OperatorRole:
    return OperatorRole.ENGINEER if stage == "request" else OperatorRole.APPROVER


def validate_admission(
    admission: OperatorAuthorizationRow | None,
    *,
    run_id: UUID,
    job_id: UUID,
    version: int,
    subject_id: str,
    command: Command,
    raw_body: str,
    occurred_at: datetime,
) -> None:
    stage = command_stage(command)
    permission = "request_approval" if stage == "request" else "decide_approval"
    model = RequestCommand if stage == "request" else DecisionCommand
    try:
        raw = raw_body.encode("utf-8")
        if (
            admission is None
            or admission.policy_version != "operator-auth-v1"
            or admission.subject_id != subject_id
            or admission.role != role(stage).value
            or admission.method != "POST"
            or admission.operation != operation(stage)
            or admission.route != BASE_ROUTE + ("/decision" if stage == "decision" else "")
            or admission.resources
            != {"run_id": str(run_id), "job_id": str(job_id), "version": str(version)}
            or permission not in admission.permissions
            or admission.authorized_at > occurred_at
            or not 2 <= len(raw) <= 65536
            or hashlib.sha256(raw).hexdigest() != admission.body_sha256
            or model.model_validate_json(raw) != command
        ):
            raise ValueError("审批正文或资源未匹配可信准入")
    except (ValueError, UnicodeError) as error:
        raise ApprovalError(
            "condition_approval_integrity_violation", "核验审批缺少同源可信准入"
        ) from error
