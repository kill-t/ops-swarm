"""调查领域错误。"""

from uuid import UUID


class InvestigationError(Exception):
    """调查模块可预期错误的基类。"""


class UnsupportedInvestigationTarget(InvestigationError):
    """固定演练数据源不支持目标服务。"""

    def __init__(self, service_name: str) -> None:
        self.service_name = service_name
        super().__init__(f"当前调查数据源不支持服务：{service_name}")


class MissingStructuredConclusion(InvestigationError):
    """模型运行结束但没有产生约定的结构化结论。"""


class UnverifiedEvidenceReference(InvestigationError):
    """模型引用了本轮工具没有返回的证据。"""

    def __init__(self, evidence_ids: set[str]) -> None:
        self.evidence_ids = frozenset(evidence_ids)
        joined_ids = "、".join(sorted(evidence_ids))
        super().__init__(f"调查结论引用了未经工具返回的证据：{joined_ids}")


class InvestigationRunNotFound(InvestigationError):
    """目标调查运行不存在。"""

    def __init__(self, run_id: UUID) -> None:
        self.run_id = run_id
        super().__init__(f"调查运行不存在：{run_id}")


class WorkItemNotFound(InvestigationError):
    """目标持久任务不存在。"""

    def __init__(self, work_item_id: UUID) -> None:
        self.work_item_id = work_item_id
        super().__init__(f"持久任务不存在：{work_item_id}")


class WorkItemLeaseLost(InvestigationError):
    """Worker 持有的租约已过期或已被其他实例接管。"""

    def __init__(self, work_item_id: UUID) -> None:
        self.work_item_id = work_item_id
        super().__init__(f"持久任务租约已经失效：{work_item_id}")


class SpecialistKindMismatch(InvestigationError):
    """专业节点返回了不属于自身职责的报告类型。"""


class IncompleteSpecialistReports(InvestigationError):
    """综合节点没有收到全部专业调查报告。"""


class InvestigationToolBudgetExceeded(InvestigationError):
    """模型尝试调用只读工具的次数超过显式预算。"""


class InvestigationDataSourceFailure(InvestigationError):
    """只读调查数据源未能返回可验证的规范化观察。"""

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


class GraphThreadIdMismatch(InvestigationError):
    """业务运行已经关联到另一个不符合确定性规则的图线程。"""
