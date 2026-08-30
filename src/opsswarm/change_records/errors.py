"""变更记录模拟器的有限业务错误。"""


class ChangeRecordError(Exception):
    """变更记录模块可预期错误的基类。"""


class ChangeRecordIdempotencyConflict(ChangeRecordError):
    """同一外部变更号被用于不同内容。"""

    def __init__(self, external_change_id: str) -> None:
        self.external_change_id = external_change_id
        super().__init__(f"外部变更号已存在但内容不同：{external_change_id}")
