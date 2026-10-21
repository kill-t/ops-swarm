"""绑定可信准入、实际请求正文与规范业务命令。"""

import hashlib
from datetime import datetime
from uuid import UUID

from pydantic import TypeAdapter

from opsswarm.identity.tables import OperatorAuthorizationRow
from opsswarm.investigation.human_wait.contracts import (
    MAX_COMMAND_BYTES,
    HumanWaitError,
    WaitCommand,
)

_COMMAND: TypeAdapter[WaitCommand] = TypeAdapter(WaitCommand)


def parse_command(value: object) -> WaitCommand:
    return _COMMAND.validate_python(value)


def operation_for(action: str) -> str:
    return (
        "supplement_investigation_wait" if action == "supplement" else "cancel_investigation_wait"
    )


def route_for(run_id: UUID, action: str) -> str:
    suffix = "supplements" if action == "supplement" else "cancel"
    return f"/api/v1/investigations/{{run_id}}/human-wait/{suffix}"


def validate_admission(
    admission: OperatorAuthorizationRow | None,
    *,
    run_id: UUID,
    subject_id: str,
    command: WaitCommand,
    raw_body: str,
    occurred_at: datetime,
) -> None:
    """历史事实验证原提交时的准入；当前凭据到期由命令服务另行检查。"""

    try:
        raw = raw_body.encode("utf-8")
        if (
            admission is None
            or admission.policy_version != "operator-auth-v1"
            or admission.subject_id != subject_id
            or admission.role != "engineer"
            or admission.method != "POST"
            or admission.operation != operation_for(command.action)
            or admission.route != route_for(run_id, command.action)
            or admission.resources != {"run_id": str(run_id)}
            or "manage_incident" not in admission.permissions
            or admission.authorized_at > occurred_at
            or not 2 <= len(raw) <= MAX_COMMAND_BYTES
            or hashlib.sha256(raw).hexdigest() != admission.body_sha256
            or type(command).model_validate_json(raw) != command
        ):
            raise ValueError("授权与命令不一致")
    except (ValueError, UnicodeError) as error:
        raise HumanWaitError(
            "human_wait_integrity_violation", "人工命令缺少同源可信授权"
        ) from error
