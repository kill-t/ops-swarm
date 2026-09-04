"""复盘改进行动项纯状态机测试。"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from opsswarm.follow_up import (
    ReportActionItem,
    ReportActionItemAlreadyCompleted,
    ReportActionItemStatus,
    ReportActionItemValidationError,
    ReportActionItemVersionConflict,
    complete_report_action_item,
    create_report_action_item,
)

NOW = datetime(2026, 9, 4, 8, 0, tzinfo=UTC)


def make_item() -> ReportActionItem:
    """创建供状态迁移测试复用的最小合法行动项。"""

    return create_report_action_item(
        item_id=uuid4(),
        report_id=uuid4(),
        idempotency_key="follow-up-001",
        title=" 补充 Redis 故障演练 ",
        description=" 固化依赖恢复验证步骤 ",
        owner_id=" sre-001 ",
        due_at=NOW + timedelta(days=7),
        created_by_id=" engineer-001 ",
        occurred_at=NOW,
    )


def test_create_report_action_item_normalizes_text_and_requires_future_due_time() -> None:
    item = make_item()

    assert item.status is ReportActionItemStatus.OPEN
    assert item.version == 1
    assert item.title == "补充 Redis 故障演练"
    assert item.owner_id == "sre-001"

    with pytest.raises(ReportActionItemValidationError):
        create_report_action_item(
            item_id=uuid4(),
            report_id=uuid4(),
            idempotency_key="follow-up-002",
            title="补测试",
            description="截止时间不能等于创建时间",
            owner_id="sre-001",
            due_at=NOW,
            created_by_id="engineer-001",
            occurred_at=NOW,
        )


def test_complete_report_action_item_is_versioned_and_exact_retry_is_idempotent() -> None:
    item = make_item()

    completed, applied = complete_report_action_item(
        item,
        expected_version=1,
        completed_by_id=" engineer-002 ",
        completion_note=" 已增加演练并通过验证 ",
        occurred_at=NOW + timedelta(days=1),
    )
    retried, retry_applied = complete_report_action_item(
        completed,
        expected_version=1,
        completed_by_id="engineer-002",
        completion_note="已增加演练并通过验证",
        occurred_at=NOW + timedelta(days=2),
    )

    assert applied is True
    assert completed.status is ReportActionItemStatus.COMPLETED
    assert completed.version == 2
    assert retry_applied is False
    assert retried == completed


def test_complete_report_action_item_rejects_stale_or_different_completion() -> None:
    item = make_item()

    with pytest.raises(ReportActionItemVersionConflict):
        complete_report_action_item(
            item,
            expected_version=2,
            completed_by_id="engineer-002",
            completion_note="完成",
            occurred_at=NOW + timedelta(days=1),
        )

    completed, _ = complete_report_action_item(
        item,
        expected_version=1,
        completed_by_id="engineer-002",
        completion_note="完成",
        occurred_at=NOW + timedelta(days=1),
    )
    with pytest.raises(ReportActionItemAlreadyCompleted):
        complete_report_action_item(
            completed,
            expected_version=2,
            completed_by_id="engineer-003",
            completion_note="试图改写审计",
            occurred_at=NOW + timedelta(days=2),
        )
