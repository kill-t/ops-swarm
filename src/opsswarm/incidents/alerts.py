"""告警接收领域数据。"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from opsswarm.incidents.errors import IncidentValidationError
from opsswarm.incidents.models import (
    Incident,
    IncidentSeverity,
    normalize_required_text,
    normalize_utc,
)

MAX_ALERT_PAYLOAD_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class AlertSubmission:
    """经过领域边界校验、等待持久化的外部告警。"""

    source: str
    external_event_id: str
    fingerprint: str
    service_name: str
    severity: IncidentSeverity
    title: str
    payload: Mapping[str, Any]
    occurred_at: datetime
    ingested_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.severity, IncidentSeverity):
            raise IncidentValidationError("severity 必须是 IncidentSeverity")
        if not isinstance(self.payload, Mapping):
            raise IncidentValidationError("payload 必须是 JSON 对象")

        payload = dict(self.payload)
        try:
            encoded_payload = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode()
        except (TypeError, ValueError) as error:
            raise IncidentValidationError("payload 必须能序列化为标准 JSON") from error
        if len(encoded_payload) > MAX_ALERT_PAYLOAD_BYTES:
            raise IncidentValidationError(
                f"payload 序列化后不能超过 {MAX_ALERT_PAYLOAD_BYTES} 字节"
            )
        normalized_payload = json.loads(encoded_payload)
        if not isinstance(normalized_payload, dict):
            raise IncidentValidationError("payload 必须是 JSON 对象")

        object.__setattr__(
            self,
            "source",
            normalize_required_text(self.source, field_name="source", max_length=100),
        )
        object.__setattr__(
            self,
            "external_event_id",
            normalize_required_text(
                self.external_event_id,
                field_name="external_event_id",
                max_length=200,
            ),
        )
        object.__setattr__(
            self,
            "fingerprint",
            normalize_required_text(self.fingerprint, field_name="fingerprint", max_length=200),
        )
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
        object.__setattr__(self, "payload", normalized_payload)
        object.__setattr__(
            self,
            "occurred_at",
            normalize_utc(self.occurred_at, field_name="occurred_at"),
        )
        object.__setattr__(
            self,
            "ingested_at",
            normalize_utc(self.ingested_at, field_name="ingested_at"),
        )


@dataclass(frozen=True, slots=True)
class AlertIngestionResult:
    """告警接收后的稳定业务结果。"""

    incident: Incident
    display_number: int
    alert_id: UUID
    deduplicated: bool
    created_incident: bool

    def __post_init__(self) -> None:
        if self.display_number < 1:
            raise IncidentValidationError("display_number 必须大于等于 1")
        if self.deduplicated and self.created_incident:
            raise IncidentValidationError("重复告警不能同时创建 Incident")
