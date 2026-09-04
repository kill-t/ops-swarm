"""复盘改进行动项领域错误。"""

from uuid import UUID


class ReportActionItemError(Exception):
    """复盘改进行动项错误的共同基类。"""


class ReportActionItemValidationError(ReportActionItemError, ValueError):
    """行动项命令或状态不满足确定性约束。"""


class FollowUpReportNotFound(ReportActionItemError):
    """目标事故复盘报告不存在。"""

    def __init__(self, report_id: UUID) -> None:
        self.report_id = report_id
        super().__init__(f"事故复盘报告不存在：{report_id}")


class ReportActionItemNotFound(ReportActionItemError):
    """目标改进行动项不存在。"""

    def __init__(self, item_id: UUID) -> None:
        self.item_id = item_id
        super().__init__(f"复盘改进行动项不存在：{item_id}")


class ReportActionItemVersionConflict(ReportActionItemError):
    """行动项乐观锁版本已经变化。"""

    def __init__(self, *, expected_version: int, actual_version: int) -> None:
        self.expected_version = expected_version
        self.actual_version = actual_version
        super().__init__(
            f"改进行动项版本冲突：期望版本 {expected_version}，实际版本 {actual_version}"
        )


class ReportActionItemIdempotencyConflict(ReportActionItemError):
    """同一幂等键已经绑定不同内容。"""

    def __init__(self, report_id: UUID, idempotency_key: str) -> None:
        self.report_id = report_id
        self.idempotency_key = idempotency_key
        super().__init__(f"幂等键已绑定不同改进行动项：{idempotency_key}")


class ReportActionItemAlreadyCompleted(ReportActionItemError):
    """已完成行动项收到内容不同的再次完成命令。"""

    def __init__(self, item_id: UUID) -> None:
        self.item_id = item_id
        super().__init__(f"改进行动项已经完成，不能改写完成审计：{item_id}")
