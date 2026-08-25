"""审批请求领域的可预期错误。"""

from uuid import UUID


class ApprovalError(Exception):
    """审批模块可预期错误的基类。"""


class ApprovalActionPlanNotFound(ApprovalError):
    """目标计划不存在。"""

    def __init__(self, plan_id: UUID) -> None:
        self.plan_id = plan_id
        super().__init__(f"处置计划不存在：{plan_id}")


class ApprovalRequestNotFound(ApprovalError):
    """目标审批请求不存在。"""

    def __init__(self, request_id: UUID) -> None:
        self.request_id = request_id
        super().__init__(f"审批请求不存在：{request_id}")


class ActionPlanNotCurrent(ApprovalError):
    """调用方尝试提交已经被新版本替代的计划。"""

    def __init__(self, plan_id: UUID, *, current_plan_id: UUID) -> None:
        self.plan_id = plan_id
        self.current_plan_id = current_plan_id
        super().__init__(f"处置计划不是当前版本：{plan_id}")


class ActionPlanDoesNotRequireApproval(ApprovalError):
    """计划没有审批要求，不应创建人工审批。"""


class ActionPlanNotSubmittable(ApprovalError):
    """计划当前状态不能创建新的审批请求。"""

    def __init__(self, plan_id: UUID, *, status: str) -> None:
        self.plan_id = plan_id
        self.status = status
        super().__init__(f"处置计划状态为 {status}，不能创建审批请求")


class ApprovalRequesterMustBeHuman(ApprovalError):
    """当前切片只接受显式人工请求人。"""


class ApprovalPlanIntegrityViolation(ApprovalError):
    """当前计划内容与审批请求保存的摘要不一致。"""

    def __init__(self, request_id: UUID) -> None:
        self.request_id = request_id
        super().__init__(f"审批请求绑定的计划内容已发生变化：{request_id}")
