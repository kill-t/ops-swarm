"""审批请求状态与策略常量。"""

from datetime import timedelta
from enum import StrEnum

APPROVAL_REQUEST_TTL = timedelta(minutes=30)


class ApprovalRequestStatus(StrEnum):
    """当前切片只创建等待人工决定的请求。"""

    PENDING = "pending"
