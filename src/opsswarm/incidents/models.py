"""Incident 领域值对象与聚合数据。"""

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from opsswarm.incidents.errors import IncidentValidationError


class IncidentStatus(StrEnum):
    """Incident 生命周期状态。"""

    OPEN = "open"
    INVESTIGATING = "investigating"
    AWAITING_APPROVAL = "awaiting_approval"
    MITIGATING = "mitigating"
    VERIFYING = "verifying"
    RESOLVED = "resolved"
    CLOSED = "closed"


class IncidentSeverity(StrEnum):
    """Incident 严重级别。"""

    P1 = "p1"
    P2 = "p2"
    P3 = "p3"
    P4 = "p4"


class IncidentAction(StrEnum):
    """驱动状态迁移的稳定业务动作。"""

    START_INVESTIGATION = "start_investigation"
    REQUEST_APPROVAL = "request_approval"
    START_VERIFICATION = "start_verification"
    APPROVE_PLAN = "approve_plan"
    REJECT_PLAN = "reject_plan"
    EXPIRE_APPROVAL = "expire_approval"
    COMPLETE_MITIGATION = "complete_mitigation"
    FAIL_MITIGATION = "fail_mitigation"
    CONFIRM_RECOVERY = "confirm_recovery"
    FAIL_VERIFICATION = "fail_verification"
    CLOSE = "close"
    REOPEN = "reopen"


class ActorType(StrEnum):
    """审计事件的操作者类型。"""

    SYSTEM = "system"
    HUMAN = "human"
    AGENT = "agent"
    EXECUTOR = "executor"


class IncidentEventType(StrEnum):
    """当前切片支持的 Incident 领域事件。"""

    CREATED = "incident.created"
    STATUS_CHANGED = "incident.status_changed"
    ALERT_ATTACHED = "incident.alert_attached"


def normalize_utc(value: datetime, *, field_name: str) -> datetime:
    """拒绝无时区时间，并把有效时间统一转换为 UTC。"""

    if not isinstance(value, datetime):
        raise IncidentValidationError(f"{field_name} 必须是 datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise IncidentValidationError(f"{field_name} 必须包含时区")
    return value.astimezone(UTC)


def normalize_required_text(value: str, *, field_name: str, max_length: int) -> str:
    if not isinstance(value, str):
        raise IncidentValidationError(f"{field_name} 必须是字符串")
    normalized = value.strip()
    if not normalized:
        raise IncidentValidationError(f"{field_name} 不能为空")
    if len(normalized) > max_length:
        raise IncidentValidationError(f"{field_name} 长度不能超过 {max_length}")
    return normalized


@dataclass(frozen=True, slots=True)
class IncidentActor:
    """记录谁触发了领域行为，不承担身份认证职责。"""

    actor_type: ActorType
    actor_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.actor_type, ActorType):
            raise IncidentValidationError("actor_type 必须是 ActorType")

        actor_id = self.actor_id.strip() if isinstance(self.actor_id, str) else self.actor_id
        if actor_id == "":
            actor_id = None
        if actor_id is not None and not isinstance(actor_id, str):
            raise IncidentValidationError("actor_id 必须是字符串或 None")
        if actor_id is not None and len(actor_id) > 100:
            raise IncidentValidationError("actor_id 长度不能超过 100")
        if self.actor_type is not ActorType.SYSTEM and actor_id is None:
            raise IncidentValidationError("非系统操作者必须提供 actor_id")

        object.__setattr__(self, "actor_id", actor_id)


@dataclass(frozen=True, slots=True)
class Incident:
    """Incident 聚合在某一版本的不可变快照。"""

    id: UUID
    service_name: str
    title: str
    severity: IncidentSeverity
    status: IncidentStatus
    fingerprint: str
    version: int
    started_at: datetime
    resolved_at: datetime | None
    closed_at: datetime | None
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.id, UUID):
            raise IncidentValidationError("id 必须是 UUID")
        if not isinstance(self.severity, IncidentSeverity):
            raise IncidentValidationError("severity 必须是 IncidentSeverity")
        if not isinstance(self.status, IncidentStatus):
            raise IncidentValidationError("status 必须是 IncidentStatus")
        if not isinstance(self.version, int) or isinstance(self.version, bool) or self.version < 1:
            raise IncidentValidationError("version 必须是大于等于 1 的整数")

        object.__setattr__(
            self,
            "service_name",
            normalize_required_text(self.service_name, field_name="service_name", max_length=100),
        )
        object.__setattr__(
            self,
            "title",
            normalize_required_text(self.title, field_name="title", max_length=200),
        )
        object.__setattr__(
            self,
            "fingerprint",
            normalize_required_text(self.fingerprint, field_name="fingerprint", max_length=200),
        )

        started_at = normalize_utc(self.started_at, field_name="started_at")
        created_at = normalize_utc(self.created_at, field_name="created_at")
        updated_at = normalize_utc(self.updated_at, field_name="updated_at")
        resolved_at = (
            normalize_utc(self.resolved_at, field_name="resolved_at")
            if self.resolved_at is not None
            else None
        )
        closed_at = (
            normalize_utc(self.closed_at, field_name="closed_at")
            if self.closed_at is not None
            else None
        )

        if created_at > updated_at:
            raise IncidentValidationError("created_at 不能晚于 updated_at")
        if self.status in {IncidentStatus.RESOLVED, IncidentStatus.CLOSED}:
            if resolved_at is None:
                raise IncidentValidationError("resolved 或 closed 状态必须记录 resolved_at")
        elif resolved_at is not None:
            raise IncidentValidationError("未解决状态不能保留 resolved_at")
        if resolved_at is not None and resolved_at > updated_at:
            raise IncidentValidationError("resolved_at 不能晚于 updated_at")
        if self.status is IncidentStatus.CLOSED:
            if closed_at is None:
                raise IncidentValidationError("closed 状态必须记录 closed_at")
        elif closed_at is not None:
            raise IncidentValidationError("非 closed 状态不能记录 closed_at")
        if closed_at is not None:
            if closed_at > updated_at:
                raise IncidentValidationError("closed_at 不能晚于 updated_at")
            if resolved_at is not None and closed_at < resolved_at:
                raise IncidentValidationError("closed_at 不能早于 resolved_at")

        object.__setattr__(self, "started_at", started_at)
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "updated_at", updated_at)
        object.__setattr__(self, "resolved_at", resolved_at)
        object.__setattr__(self, "closed_at", closed_at)


@dataclass(frozen=True, slots=True)
class IncidentEvent:
    """与 Incident 版本一起产生的不可变审计事件。"""

    event_type: IncidentEventType
    incident_id: UUID
    actor: IncidentActor
    from_status: IncidentStatus | None
    to_status: IncidentStatus
    reason: str
    occurred_at: datetime
    incident_version: int

    def __post_init__(self) -> None:
        if not isinstance(self.event_type, IncidentEventType):
            raise IncidentValidationError("event_type 必须是 IncidentEventType")
        if not isinstance(self.incident_id, UUID):
            raise IncidentValidationError("incident_id 必须是 UUID")
        if not isinstance(self.actor, IncidentActor):
            raise IncidentValidationError("actor 必须是 IncidentActor")
        if self.from_status is not None and not isinstance(self.from_status, IncidentStatus):
            raise IncidentValidationError("from_status 必须是 IncidentStatus 或 None")
        if not isinstance(self.to_status, IncidentStatus):
            raise IncidentValidationError("to_status 必须是 IncidentStatus")
        if (
            not isinstance(self.incident_version, int)
            or isinstance(self.incident_version, bool)
            or self.incident_version < 1
        ):
            raise IncidentValidationError("incident_version 必须是大于等于 1 的整数")

        if self.event_type is IncidentEventType.CREATED and self.from_status is not None:
            raise IncidentValidationError("创建事件不能有前置状态")
        if self.event_type is IncidentEventType.STATUS_CHANGED:
            if self.from_status is None:
                raise IncidentValidationError("状态变更事件必须有前置状态")
            if self.from_status is self.to_status:
                raise IncidentValidationError("状态变更事件的前后状态不能相同")

        object.__setattr__(
            self,
            "reason",
            normalize_required_text(self.reason, field_name="reason", max_length=500),
        )
        object.__setattr__(
            self,
            "occurred_at",
            normalize_utc(self.occurred_at, field_name="occurred_at"),
        )


@dataclass(frozen=True, slots=True)
class IncidentMutation:
    """要求持久化层原子保存的新快照和配套事件。"""

    incident: Incident
    event: IncidentEvent

    def __post_init__(self) -> None:
        if not isinstance(self.incident, Incident):
            raise IncidentValidationError("incident 必须是 Incident")
        if not isinstance(self.event, IncidentEvent):
            raise IncidentValidationError("event 必须是 IncidentEvent")
        if self.incident.id != self.event.incident_id:
            raise IncidentValidationError("Incident 与事件必须属于同一事故")
        if self.incident.status is not self.event.to_status:
            raise IncidentValidationError("Incident 状态必须与事件目标状态一致")
        if self.incident.version != self.event.incident_version:
            raise IncidentValidationError("Incident 版本必须与事件版本一致")
        if self.incident.updated_at != self.event.occurred_at:
            raise IncidentValidationError("Incident 更新时间必须与事件时间一致")
