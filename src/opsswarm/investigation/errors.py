"""调查领域错误。"""


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
