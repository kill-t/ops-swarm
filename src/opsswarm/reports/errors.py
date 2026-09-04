"""事故复盘报告领域错误。"""

from uuid import UUID


class IncidentReportError(Exception):
    """事故复盘报告错误的共同基类。"""


class IncidentReportNotFound(IncidentReportError):
    """目标 Incident 尚未生成复盘报告。"""

    def __init__(self, incident_id: UUID) -> None:
        self.incident_id = incident_id
        super().__init__(f"Incident 尚无复盘报告：{incident_id}")


class IncidentReportNotReady(IncidentReportError):
    """Incident 尚未关闭，不能冻结最终复盘。"""

    def __init__(self, incident_id: UUID, *, status: str) -> None:
        self.incident_id = incident_id
        self.status = status
        super().__init__(f"Incident {incident_id} 当前状态为 {status}，不能生成最终复盘报告")


class IncidentReportTooLarge(IncidentReportError):
    """时间线超过首版报告能够完整保存的上限。"""

    def __init__(self, incident_id: UUID, *, maximum: int) -> None:
        self.incident_id = incident_id
        self.maximum = maximum
        super().__init__(f"Incident {incident_id} 的时间线超过 {maximum} 条，拒绝生成不完整报告")


class IncidentReportIntegrityViolation(IncidentReportError):
    """已保存报告的内容与摘要不一致。"""

    def __init__(self, report_id: UUID) -> None:
        self.report_id = report_id
        super().__init__(f"事故复盘报告内容完整性校验失败：{report_id}")
