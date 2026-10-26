"""将执行委派绑定当前凭据版本及精确可信原始命令。"""

import hashlib
import json
from datetime import datetime
from uuid import UUID

from opsswarm.execution.release_jobs.contracts import (
    OPERATION,
    ROUTE,
    ExecutionCommand,
    ExecutionJobError,
)
from opsswarm.identity.models import OperatorAccessError, OperatorPrincipal, OperatorRole
from opsswarm.identity.settings import OperatorAuthSettings, OperatorCredential
from opsswarm.identity.tables import OperatorAuthorizationRow


def binding(credential: OperatorCredential) -> str:
    # 二次摘要仅用于检测凭据轮换，不保存令牌或环境中的secret摘要。
    raw = [
        credential.credential_id,
        credential.subject_id,
        credential.role.value,
        credential.expires_at.isoformat(),
        credential.secret_sha256,
    ]
    return hashlib.sha256(json.dumps(raw, separators=(",", ":")).encode()).hexdigest()


def credential_for(
    settings: OperatorAuthSettings,
    *,
    credential_id: str,
    subject_id: str,
    now: datetime,
    expected_binding: str | None = None,
) -> OperatorCredential:
    found = next((c for c in settings.credentials if c.credential_id == credential_id), None)
    if (
        settings.mode != "bearer"
        or found is None
        or found.subject_id != subject_id
        or found.role is not OperatorRole.EXECUTOR
        or now >= found.expires_at
        or (expected_binding is not None and binding(found) != expected_binding)
    ):
        raise OperatorAccessError("operator_unauthenticated", "执行委派的当前凭据无效或已改变")
    return found


def principal_credential(
    settings: OperatorAuthSettings, principal: OperatorPrincipal, now: datetime
) -> OperatorCredential:
    found = credential_for(
        settings, credential_id=principal.credential_id, subject_id=principal.subject_id, now=now
    )
    if principal.role is not OperatorRole.EXECUTOR or principal.expires_at != found.expires_at:
        raise OperatorAccessError("operator_permission_denied", "执行任务仅允许当前可信执行者", 403)
    return found


def validate_admission(
    admission: OperatorAuthorizationRow | None,
    *,
    run_id: UUID,
    job_id: UUID,
    version: int,
    subject_id: str,
    command: ExecutionCommand,
    raw_body: str,
    occurred_at: datetime,
) -> None:
    try:
        raw = raw_body.encode("utf-8")
        if (
            admission is None
            or admission.policy_version != "operator-auth-v1"
            or admission.role != "executor"
            or admission.subject_id != subject_id
            or admission.operation != OPERATION
            or admission.route != ROUTE
            or admission.method != "POST"
            or "execute_action" not in admission.permissions
            or admission.resources
            != dict(run_id=str(run_id), job_id=str(job_id), version=str(version))
            or admission.authorized_at > occurred_at
            or not 2 <= len(raw) <= 65536
            or admission.body_sha256 != hashlib.sha256(raw).hexdigest()
            or ExecutionCommand.model_validate_json(raw) != command
        ):
            raise ValueError("执行任务缺少精确准入")
    except (ValueError, UnicodeError) as error:
        raise ExecutionJobError(
            "release_execution_integrity_violation", "执行任务命令缺少同源可信准入"
        ) from error
