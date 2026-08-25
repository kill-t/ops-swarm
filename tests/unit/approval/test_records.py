"""审批请求终态审计模型的纯单元测试。"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from pydantic import ValidationError

from opsswarm.approval import ApprovalRequestStatus
from opsswarm.approval.persistence import StoredApprovalRequest
from opsswarm.incidents import ActorType

BASE_TIME = datetime(2026, 1, 1, 8, 0, tzinfo=UTC)


def request_data() -> dict[str, object]:
    return {
        "id": UUID("00000000-0000-0000-0000-000000000201"),
        "incident_id": UUID("00000000-0000-0000-0000-000000000202"),
        "action_plan_id": UUID("00000000-0000-0000-0000-000000000203"),
        "action_plan_version": 1,
        "plan_content_hash": "a" * 64,
        "status": ApprovalRequestStatus.PENDING,
        "requested_by_type": ActorType.HUMAN,
        "requested_by_id": "engineer-001",
        "request_reason": "请求审批回滚计划",
        "created_at": BASE_TIME,
        "expires_at": BASE_TIME + timedelta(minutes=30),
    }


def test_pending_request_has_no_decision_audit() -> None:
    request = StoredApprovalRequest.model_validate(request_data())

    assert request.status is ApprovalRequestStatus.PENDING
    assert request.decided_at is None


def test_approved_request_requires_human_decider() -> None:
    data = {
        **request_data(),
        "status": ApprovalRequestStatus.APPROVED,
        "decided_by_type": ActorType.SYSTEM,
        "decision_reason": "系统不能代替人工批准",
        "decided_at": BASE_TIME + timedelta(minutes=1),
    }

    with pytest.raises(ValidationError, match="人工操作者"):
        StoredApprovalRequest.model_validate(data)


def test_expired_request_requires_system_audit_without_actor_id() -> None:
    data = {
        **request_data(),
        "status": ApprovalRequestStatus.EXPIRED,
        "decided_by_type": ActorType.SYSTEM,
        "decision_reason": "审批请求超过 30 分钟有效期",
        "decided_at": BASE_TIME + timedelta(minutes=30),
    }

    request = StoredApprovalRequest.model_validate(data)

    assert request.status is ApprovalRequestStatus.EXPIRED
    assert request.decided_by_id is None


def test_decision_time_must_include_timezone() -> None:
    data = {
        **request_data(),
        "status": ApprovalRequestStatus.REJECTED,
        "decided_by_type": ActorType.HUMAN,
        "decided_by_id": "engineer-002",
        "decision_reason": "证据不足",
        "decided_at": datetime(2026, 1, 1, 8, 1),
    }

    with pytest.raises(ValidationError, match="必须包含时区"):
        StoredApprovalRequest.model_validate(data)
