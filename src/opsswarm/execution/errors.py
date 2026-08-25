"""确定性执行领域的可预期错误。"""

from uuid import UUID


class ExecutionError(Exception):
    """执行模块可预期错误基类。"""


class OperationStepNotFound(ExecutionError):
    def __init__(self, step_id: UUID) -> None:
        self.step_id = step_id
        super().__init__(f"处置步骤不存在：{step_id}")


class OperationExecutionNotFound(ExecutionError):
    def __init__(self, execution_id: UUID) -> None:
        self.execution_id = execution_id
        super().__init__(f"操作执行记录不存在：{execution_id}")


class OperationNotAuthorized(ExecutionError):
    def __init__(self, step_id: UUID, *, reason_code: str, message: str) -> None:
        self.step_id = step_id
        self.reason_code = reason_code
        super().__init__(message)


class OperationIdempotencyConflict(ExecutionError):
    def __init__(self, idempotency_key: str) -> None:
        self.idempotency_key = idempotency_key
        super().__init__(f"幂等键已经绑定其他处置步骤：{idempotency_key}")


class OperationStepAlreadyRunning(ExecutionError):
    def __init__(self, step_id: UUID, *, execution_id: UUID) -> None:
        self.step_id = step_id
        self.execution_id = execution_id
        super().__init__(f"处置步骤已有运行中的执行：{execution_id}")


class OperationStepAlreadySucceeded(ExecutionError):
    def __init__(self, step_id: UUID, *, execution_id: UUID) -> None:
        self.step_id = step_id
        self.execution_id = execution_id
        super().__init__(f"处置步骤已经成功执行：{execution_id}")


class ExecutionLeaseLost(ExecutionError):
    def __init__(self, execution_id: UUID) -> None:
        self.execution_id = execution_id
        super().__init__(f"操作执行租约已失效：{execution_id}")


class OperationExecutionFailed(ExecutionError):
    def __init__(
        self,
        execution_id: UUID,
        *,
        error_code: str,
        message: str,
    ) -> None:
        self.execution_id = execution_id
        self.error_code = error_code
        super().__init__(message)


class OperationAdapterFailure(Exception):
    """适配器只向应用层暴露脱敏后的稳定失败。"""

    def __init__(self, *, error_code: str, message: str) -> None:
        self.error_code = error_code
        super().__init__(message)
