"""复盘改进行动项的纯领域状态迁移。"""

from datetime import datetime
from uuid import UUID

from pydantic import ValidationError

from opsswarm.follow_up.errors import (
    ReportActionItemAlreadyCompleted,
    ReportActionItemValidationError,
    ReportActionItemVersionConflict,
)
from opsswarm.follow_up.models import ReportActionItem, ReportActionItemStatus


def create_report_action_item(
    *,
    item_id: UUID,
    report_id: UUID,
    idempotency_key: str,
    title: str,
    description: str,
    owner_id: str,
    due_at: datetime,
    created_by_id: str,
    occurred_at: datetime,
) -> ReportActionItem:
    """构造只能由人工 API 提交的 open 行动项。"""

    try:
        return ReportActionItem(
            id=item_id,
            report_id=report_id,
            idempotency_key=idempotency_key,
            title=title,
            description=description,
            owner_id=owner_id,
            status=ReportActionItemStatus.OPEN,
            version=1,
            due_at=due_at,
            created_by_id=created_by_id,
            created_at=occurred_at,
        )
    except ValidationError as error:
        raise ReportActionItemValidationError(str(error)) from error


def complete_report_action_item(
    item: ReportActionItem,
    *,
    expected_version: int,
    completed_by_id: str,
    completion_note: str,
    occurred_at: datetime,
) -> tuple[ReportActionItem, bool]:
    """完成 open 行动项；完全相同的重试返回原终态。"""

    completed_by_id = completed_by_id.strip()
    completion_note = completion_note.strip()
    if item.status is ReportActionItemStatus.COMPLETED:
        if item.completed_by_id == completed_by_id and item.completion_note == completion_note:
            return item, False
        raise ReportActionItemAlreadyCompleted(item.id)
    if item.version != expected_version:
        raise ReportActionItemVersionConflict(
            expected_version=expected_version,
            actual_version=item.version,
        )
    try:
        completed = item.model_dump()
        completed.update(
            {
                "status": ReportActionItemStatus.COMPLETED,
                "version": item.version + 1,
                "completed_by_id": completed_by_id,
                "completion_note": completion_note,
                "completed_at": occurred_at,
            }
        )
        return (
            ReportActionItem.model_validate(completed),
            True,
        )
    except ValidationError as error:
        raise ReportActionItemValidationError(str(error)) from error
