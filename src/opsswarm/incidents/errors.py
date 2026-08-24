"""Incident 领域错误。"""


class IncidentDomainError(Exception):
    """所有 Incident 领域错误的基类。"""


class IncidentValidationError(IncidentDomainError, ValueError):
    """领域对象或命令参数不满足业务约束。"""


class IncidentVersionConflict(IncidentDomainError):
    """调用方持有的版本已经过期。"""

    def __init__(self, *, expected_version: int, actual_version: int) -> None:
        self.expected_version = expected_version
        self.actual_version = actual_version
        super().__init__(
            f"Incident 版本冲突：期望版本 {expected_version}，实际版本 {actual_version}"
        )


class InvalidIncidentTransition(IncidentDomainError):
    """当前状态不允许执行指定动作。"""

    def __init__(self, *, status: str, action: str) -> None:
        self.status = status
        self.action = action
        super().__init__(f"Incident 状态 {status} 不允许执行动作 {action}")


class IncidentNotFound(IncidentDomainError):
    """目标 Incident 不存在。"""

    def __init__(self, incident_id: str) -> None:
        self.incident_id = incident_id
        super().__init__(f"Incident 不存在：{incident_id}")
