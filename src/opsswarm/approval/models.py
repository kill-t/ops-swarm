"""审批请求状态与策略常量。"""

from datetime import timedelta
from enum import StrEnum

APPROVAL_REQUEST_TTL = timedelta(minutes=30)


class ApprovalRequestStatus(StrEnum):
    """审批请求从等待到不可变终态的生命周期。"""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class ApprovalDecision(StrEnum):
    """人工可以提交的两种显式决定。"""

    APPROVE = "approve"
    REJECT = "reject"
