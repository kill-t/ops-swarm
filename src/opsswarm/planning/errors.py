"""处置计划领域的可预期错误。"""

from uuid import UUID

from opsswarm.investigation.models import HypothesisStatus, RootCause
from opsswarm.planning.models import ActionPlanCapability


class PlanningError(Exception):
    """处置计划模块可预期错误的基类。"""


class ActionPlanNotFound(PlanningError):
    """目标调查尚无处置计划。"""

    def __init__(self, run_id: UUID) -> None:
        self.run_id = run_id
        super().__init__(f"调查运行尚无处置计划：{run_id}")


class ActionPlanNotReady(PlanningError):
    """调查事实尚未达到计划生成条件。"""

    def __init__(self, run_id: UUID, *, reason_code: str, message: str) -> None:
        self.run_id = run_id
        self.reason_code = reason_code
        super().__init__(message)


class HypothesisNotSupported(PlanningError):
    """根因假设未通过确定性质疑。"""

    def __init__(self, status: HypothesisStatus) -> None:
        self.status = status
        super().__init__(f"根因假设状态为 {status.value}，不能生成处置计划")


class UnsupportedPlanningRootCause(PlanningError):
    """当前白名单没有目标根因的安全计划模板。"""

    def __init__(self, root_cause: RootCause) -> None:
        self.root_cause = root_cause
        super().__init__(f"当前计划模板不支持根因：{root_cause.value}")


class ActionPlanCapabilityUnavailable(PlanningError):
    """调查建议没有当前可创建的确定性计划模板。"""

    def __init__(self, capability: ActionPlanCapability) -> None:
        self.capability = capability
        super().__init__(capability.explanation)


class UnsafeRollbackTarget(PlanningError):
    """证据无法给出唯一、明确的安全回滚目标。"""

    def __init__(self) -> None:
        super().__init__("变更证据缺少有效的发布前后版本，不能安全生成回滚计划")
