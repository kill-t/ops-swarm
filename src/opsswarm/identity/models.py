"""与 HTTP 和数据库无关的身份、角色和确定性权限规则。"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class OperatorRole(StrEnum):
    VIEWER = "viewer"
    ENGINEER = "engineer"
    APPROVER = "approver"
    EXECUTOR = "executor"
    ALERT_SOURCE = "alert_source"


class OperatorPermission(StrEnum):
    READ = "read"
    INGEST_ALERT = "ingest_alert"
    MANAGE_INCIDENT = "manage_incident"
    MANAGE_REPORT = "manage_report"
    PLAN_ACTION = "plan_action"
    REQUEST_APPROVAL = "request_approval"
    DECIDE_APPROVAL = "decide_approval"
    EXECUTE_ACTION = "execute_action"
    VERIFY_ACTION = "verify_action"


ROLE_PERMISSIONS: dict[OperatorRole, frozenset[OperatorPermission]] = {
    OperatorRole.VIEWER: frozenset({OperatorPermission.READ}),
    OperatorRole.ENGINEER: frozenset(
        {
            OperatorPermission.READ,
            OperatorPermission.MANAGE_INCIDENT,
            OperatorPermission.MANAGE_REPORT,
            OperatorPermission.PLAN_ACTION,
            OperatorPermission.REQUEST_APPROVAL,
            OperatorPermission.VERIFY_ACTION,
        }
    ),
    OperatorRole.APPROVER: frozenset(
        {
            OperatorPermission.READ,
            OperatorPermission.DECIDE_APPROVAL,
        }
    ),
    OperatorRole.EXECUTOR: frozenset(
        {
            OperatorPermission.READ,
            OperatorPermission.EXECUTE_ACTION,
            OperatorPermission.VERIFY_ACTION,
        }
    ),
    OperatorRole.ALERT_SOURCE: frozenset({OperatorPermission.INGEST_ALERT}),
}

WRITE_PERMISSIONS: dict[str, OperatorPermission] = {
    "supplement_investigation_wait": OperatorPermission.MANAGE_INCIDENT,
    "cancel_investigation_wait": OperatorPermission.MANAGE_INCIDENT,
    "ingest_alert": OperatorPermission.INGEST_ALERT,
    "close_incident": OperatorPermission.MANAGE_INCIDENT,
    "start_investigation": OperatorPermission.MANAGE_INCIDENT,
    "create_incident_report": OperatorPermission.MANAGE_REPORT,
    "create_report_action_item": OperatorPermission.MANAGE_REPORT,
    "complete_report_action_item": OperatorPermission.MANAGE_REPORT,
    "create_action_plan": OperatorPermission.PLAN_ACTION,
    "create_approval_request": OperatorPermission.REQUEST_APPROVAL,
    "approve_request": OperatorPermission.DECIDE_APPROVAL,
    "reject_request": OperatorPermission.DECIDE_APPROVAL,
    "execute_action_step": OperatorPermission.EXECUTE_ACTION,
    "create_verification_run": OperatorPermission.VERIFY_ACTION,
    "record_verification_sample": OperatorPermission.VERIFY_ACTION,
    "create_experience_job": OperatorPermission.MANAGE_REPORT,
    "review_experience_candidate": OperatorPermission.MANAGE_REPORT,
    "disable_experience_entry": OperatorPermission.MANAGE_REPORT,
    "replace_experience_entry": OperatorPermission.MANAGE_REPORT,
}

TRUSTED_OPERATIONS = frozenset(
    {
        "get_operator_identity",
        "get_investigation_wait",
        "list_investigation_wait_commands",
        "supplement_investigation_wait",
        "cancel_investigation_wait",
        "create_experience_job",
        "get_experience_job",
        "get_experience_candidate",
        "review_experience_candidate",
        "get_experience_review",
        "get_experience_entry",
        "disable_experience_entry",
        "replace_experience_entry",
        "get_experience_entry_state",
        "search_experience_entries",
    }
)

HUMAN_OPERATIONS = frozenset(
    {
        "close_incident",
        "start_investigation",
        "create_incident_report",
        "create_report_action_item",
        "complete_report_action_item",
        "create_approval_request",
        "approve_request",
        "reject_request",
    }
)


@dataclass(frozen=True)
class OperatorPrincipal:
    subject_id: str
    credential_id: str
    role: OperatorRole
    expires_at: datetime

    @property
    def permissions(self) -> tuple[OperatorPermission, ...]:
        return tuple(sorted(ROLE_PERMISSIONS[self.role]))


class OperatorAccessError(ValueError):
    """只携带稳定公开错误，不携带令牌或认证配置。"""

    def __init__(self, code: str, message: str, status_code: int = 401) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def require_permission(principal: OperatorPrincipal, method: str, operation: str) -> None:
    permission = OperatorPermission.READ if method == "GET" else WRITE_PERMISSIONS.get(operation)
    if permission is None or permission not in principal.permissions:
        raise OperatorAccessError("operator_permission_denied", "操作者没有该操作权限", 403)
