"""确定性恢复验证的可预期错误。"""

from uuid import UUID


class VerificationError(Exception):
    """恢复验证模块可预期错误基类。"""


class VerificationRunNotFound(VerificationError):
    def __init__(self, run_id: UUID) -> None:
        self.run_id = run_id
        super().__init__(f"恢复验证运行不存在：{run_id}")


class VerificationWorkItemNotFound(VerificationError):
    def __init__(self, work_item_id: UUID) -> None:
        self.work_item_id = work_item_id
        super().__init__(f"恢复验证调度任务不存在：{work_item_id}")


class VerificationWorkItemLeaseLost(VerificationError):
    def __init__(self, work_item_id: UUID) -> None:
        self.work_item_id = work_item_id
        super().__init__(f"恢复验证调度任务租约已经失效：{work_item_id}")


class OperationExecutionNotVerifiable(VerificationError):
    def __init__(self, execution_id: UUID, *, reason_code: str, message: str) -> None:
        self.execution_id = execution_id
        self.reason_code = reason_code
        super().__init__(message)


class VerificationRunCompleted(VerificationError):
    def __init__(self, run_id: UUID, *, status: str) -> None:
        self.run_id = run_id
        self.status = status
        super().__init__(f"恢复验证运行已经结束：{status}")


class UnsupportedVerificationRule(VerificationError):
    def __init__(self, *, signal: str, operator: str) -> None:
        self.signal = signal
        self.operator = operator
        super().__init__(f"不支持恢复验证规则：{signal} {operator}")


class VerificationProbeFailure(Exception):
    """探针边界只向应用层返回脱敏错误。"""

    def __init__(
        self,
        *,
        error_code: str,
        message: str,
        http_status: int | None = None,
    ) -> None:
        self.error_code = error_code
        self.http_status = http_status
        super().__init__(message)
