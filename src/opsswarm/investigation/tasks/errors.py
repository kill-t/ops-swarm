"""任务事实写入时可识别的状态、身份与租约冲突。"""


class TaskFactConflict(Exception):
    """内部应用服务冲突，错误码稳定且不包含模型或数据库内容。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)
