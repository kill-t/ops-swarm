"""业务角色独立验收权限矩阵，默认拒绝未登记写操作。"""

from datetime import UTC, datetime

import pytest

from opsswarm.identity.models import (
    OperatorAccessError,
    OperatorPrincipal,
    OperatorRole,
    require_permission,
)

OPERATIONS = (
    "ingest_alert",
    "close_incident",
    "start_investigation",
    "create_incident_report",
    "create_report_action_item",
    "complete_report_action_item",
    "create_action_plan",
    "create_approval_request",
    "approve_request",
    "reject_request",
    "execute_action_step",
    "create_verification_run",
    "record_verification_sample",
)

ALLOWED_WRITES = {
    OperatorRole.VIEWER: frozenset(),
    OperatorRole.ENGINEER: frozenset(
        {
            "close_incident",
            "start_investigation",
            "create_incident_report",
            "create_report_action_item",
            "complete_report_action_item",
            "create_action_plan",
            "create_approval_request",
            "create_verification_run",
            "record_verification_sample",
        }
    ),
    OperatorRole.APPROVER: frozenset({"approve_request", "reject_request"}),
    OperatorRole.EXECUTOR: frozenset(
        {
            "execute_action_step",
            "create_verification_run",
            "record_verification_sample",
        }
    ),
    OperatorRole.ALERT_SOURCE: frozenset({"ingest_alert"}),
}


def principal_for(role: OperatorRole) -> OperatorPrincipal:
    return OperatorPrincipal(
        subject_id=f"subject-{role.value}",
        credential_id=f"credential-{role.value}",
        role=role,
        expires_at=datetime(2026, 10, 20, 9, tzinfo=UTC),
    )


@pytest.mark.parametrize("role", list(OperatorRole))
@pytest.mark.parametrize("operation", OPERATIONS)
def test_role_permissions_for_each_registered_business_write(
    role: OperatorRole,
    operation: str,
) -> None:
    principal = principal_for(role)

    if operation in ALLOWED_WRITES[role]:
        require_permission(principal, "POST", operation)
    else:
        with pytest.raises(OperatorAccessError) as captured:
            require_permission(principal, "POST", operation)
        assert captured.value.status_code == 403
        assert captured.value.code == "operator_permission_denied"


@pytest.mark.parametrize("role", list(OperatorRole))
@pytest.mark.parametrize(
    "operation",
    ["get_incident", "stream_incident_events", "read_model_input"],
)
def test_alert_source_cannot_read_control_plane_materials(
    role: OperatorRole,
    operation: str,
) -> None:
    if role is OperatorRole.ALERT_SOURCE:
        with pytest.raises(OperatorAccessError) as captured:
            require_permission(principal_for(role), "GET", operation)
        assert captured.value.status_code == 403
    else:
        require_permission(principal_for(role), "GET", operation)


@pytest.mark.parametrize("role", list(OperatorRole))
@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize("operation", ["", "future_write_operation", "read_model_input"])
def test_unknown_write_operations_are_denied_for_every_role(
    role: OperatorRole,
    method: str,
    operation: str,
) -> None:
    with pytest.raises(OperatorAccessError) as captured:
        require_permission(principal_for(role), method, operation)

    assert captured.value.status_code == 403
    assert captured.value.code == "operator_permission_denied"
