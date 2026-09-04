"""确定性事故复盘报告的稳定结构。"""

from datetime import UTC, datetime
from typing import Final, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

from opsswarm.incidents import ActorType, IncidentEventType, IncidentSeverity, IncidentStatus
from opsswarm.workroom import WorkroomSnapshot

REPORT_SCHEMA_VERSION: Final[Literal["incident-report.v1"]] = "incident-report.v1"


class StrictReportModel(BaseModel):
    """复盘快照拒绝额外字段并保持不可变。"""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class IncidentReportIncident(StrictReportModel):
    """报告生成时冻结的 Incident 最终状态。"""

    id: UUID
    display_number: int = Field(ge=1)
    reference: str
    service_name: str
    title: str
    severity: IncidentSeverity
    status: Literal[IncidentStatus.CLOSED]
    fingerprint: str
    version: int = Field(ge=1)
    started_at: datetime
    resolved_at: datetime
    closed_at: datetime
    duration_seconds: int = Field(ge=0)

    @field_validator("started_at", "resolved_at", "closed_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("事故报告时间必须包含时区")
        return value.astimezone(UTC)


class IncidentReportTimelineEntry(StrictReportModel):
    """报告中冻结的一条追加式事故时间线事实。"""

    cursor: int = Field(ge=1)
    event_type: IncidentEventType
    actor_type: ActorType
    actor_id: str | None
    from_status: IncidentStatus | None
    to_status: IncidentStatus
    reason: str
    incident_version: int = Field(ge=1)
    payload: dict[str, JsonValue]
    occurred_at: datetime

    @field_validator("occurred_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("时间线时间必须包含时区")
        return value.astimezone(UTC)


class IncidentReportContent(StrictReportModel):
    """只由已持久化业务事实生成的复盘内容。"""

    schema_version: Literal["incident-report.v1"] = REPORT_SCHEMA_VERSION
    generated_at: datetime
    generated_by_id: str = Field(min_length=1, max_length=100)
    incident: IncidentReportIncident
    workroom: WorkroomSnapshot
    timeline: tuple[IncidentReportTimelineEntry, ...] = Field(min_length=1, max_length=500)

    @field_validator("generated_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("报告生成时间必须包含时区")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_snapshot_consistency(self) -> Self:
        if self.workroom.incident_id != self.incident.id:
            raise ValueError("作战室快照必须属于报告 Incident")
        if self.workroom.incident_version != self.incident.version:
            raise ValueError("作战室快照版本必须等于报告 Incident 版本")
        if self.timeline[-1].to_status is not IncidentStatus.CLOSED:
            raise ValueError("复盘时间线必须以 closed 状态结束")
        if self.timeline[-1].incident_version != self.incident.version:
            raise ValueError("复盘时间线最终版本必须等于报告 Incident 版本")
        return self


class StoredIncidentReport(StrictReportModel):
    """数据库中不可变报告快照及其完整性摘要。"""

    id: UUID
    incident_id: UUID
    incident_version: int = Field(ge=1)
    schema_version: Literal["incident-report.v1"]
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    content: IncidentReportContent
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("报告持久化时间必须包含时区")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        if self.content.incident.id != self.incident_id:
            raise ValueError("报告内容与记录 Incident 不一致")
        if self.content.incident.version != self.incident_version:
            raise ValueError("报告内容与记录版本不一致")
        if self.content.schema_version != self.schema_version:
            raise ValueError("报告内容与记录 schema 版本不一致")
        return self


class IncidentReportCreation(StrictReportModel):
    """区分首次冻结和幂等读取的生成结果。"""

    created: bool
    report: StoredIncidentReport
