"""把本地 Loki 白名单日志转换为规范化调查证据。"""

from __future__ import annotations

import hashlib
import json
import time
from collections import defaultdict
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

import httpx2
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from opsswarm.investigation.errors import (
    InvestigationDataSourceFailure,
    UnsupportedInvestigationTarget,
)
from opsswarm.investigation.models import EvidenceKind, EvidenceObservation

DEFAULT_LOKI_BASE_URL = "http://127.0.0.1:13100"
ALLOWED_LOKI_HOSTS = frozenset({"127.0.0.1", "localhost", "loki"})
SUPPORTED_SERVICE = "order-service"
LOKI_LOG_QUERY = (
    '{service="order-service",source="fault-lab",service_name=""}'
    ' |= "\\"event\\":\\"order_request_failed\\""'
)
MAX_LOG_ENTRIES = 100
MAX_LOG_LINE_BYTES = 2 * 1024
MAX_TIMESTAMP_DIFFERENCE_SECONDS = 1.0
MAX_FUTURE_CLOCK_SKEW_SECONDS = 5.0


class LokiInvestigationSettings(BaseSettings):
    """限制调查适配器只能访问本地故障实验室 Loki。"""

    model_config = SettingsConfigDict(env_prefix="OPSSWARM_INVESTIGATION_", extra="ignore")

    loki_base_url: str = Field(default=DEFAULT_LOKI_BASE_URL, min_length=1)
    request_timeout_seconds: float = Field(default=3.0, gt=0, le=10)

    @field_validator("loki_base_url")
    @classmethod
    def restrict_loki_url(cls, value: str) -> str:
        normalized = value.rstrip("/")
        parsed = urlsplit(normalized)
        if parsed.scheme != "http":
            raise ValueError("Loki 调查地址只允许 http")
        if parsed.hostname not in ALLOWED_LOKI_HOSTS:
            raise ValueError("Loki 调查地址只允许 localhost、127.0.0.1 或 loki")
        if (
            parsed.hostname in {"localhost", "127.0.0.1"}
            and parsed.port != 13100
            or parsed.hostname == "loki"
            and parsed.port != 3100
        ):
            raise ValueError("Loki 调查地址端口必须匹配本机 13100 或 Compose 3100")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("Loki 调查地址不能包含用户信息")
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ValueError("Loki 调查地址不能包含路径、查询参数或片段")
        return normalized


class _LokiStream(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stream: dict[str, str]
    values: list[tuple[str, str]]


class _LokiStreamsData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resultType: Literal["streams"]
    result: list[_LokiStream]
    stats: dict[str, JsonValue] = Field(default_factory=dict)


class _LokiQueryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["success"]
    data: _LokiStreamsData


class _BaseFailureLog(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    timestamp: datetime
    event: Literal["order_request_failed"]
    fault: str
    level: Literal["warning"]
    route: Literal["/orders/{id}"]
    service: Literal["order-service"]
    status_code: Literal[500]

    @field_validator("timestamp")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("日志时间必须包含时区")
        return value.astimezone(UTC)


class _ReleaseFailureLog(_BaseFailureLog):
    fault: Literal["release_500"]
    error_type: Literal["RuntimeError"]
    release_version: str = Field(min_length=1, max_length=100)


class _DatabasePoolFailureLog(_BaseFailureLog):
    fault: Literal["database_pool_timeout"]
    error_type: Literal["database_pool_timeout"]
    database_pool_size: int = Field(ge=1, le=1000)
    database_pool_waiters: int = Field(ge=1, le=1000)


class _RedisFailureLog(_BaseFailureLog):
    fault: Literal["redis_unavailable"]
    error_type: Literal["redis_connection_unavailable"]
    dependency: Literal["redis"]


_FailureLog = Annotated[
    _ReleaseFailureLog | _DatabasePoolFailureLog | _RedisFailureLog,
    Field(discriminator="fault"),
]
_FAILURE_LOG_ADAPTER: TypeAdapter[_FailureLog] = TypeAdapter(_FailureLog)


class _ObservedFailure(BaseModel):
    """通过 Loki 时间与 JSON 契约双重校验的失败事件。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    loki_timestamp_ns: int
    event: _FailureLog
    raw_line: str

    @model_validator(mode="after")
    def validate_timestamp_match(self) -> Self:
        loki_time = datetime.fromtimestamp(self.loki_timestamp_ns / 1_000_000_000, UTC)
        if (
            abs((self.event.timestamp - loki_time).total_seconds())
            > MAX_TIMESTAMP_DIFFERENCE_SECONDS
        ):
            raise ValueError("Loki 时间与日志内事件时间不一致")
        return self


class LokiOnlyInvestigationDataSource:
    """读取真实日志；尚未接入的指标和变更端口明确返回空集合。"""

    def __init__(
        self,
        settings: LokiInvestigationSettings | None = None,
        *,
        transport: httpx2.BaseTransport | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._settings = settings if settings is not None else LokiInvestigationSettings()
        self._transport = transport
        self._clock = clock

    def query_logs(
        self,
        *,
        service_name: str,
        lookback_minutes: int,
    ) -> Sequence[EvidenceObservation]:
        """执行固定 LogQL，并按故障分类生成至多三条聚合日志证据。"""

        self._validate_target(service_name)
        self._validate_minutes(lookback_minutes)
        end_ns = int(self._clock() * 1_000_000_000)
        start_ns = end_ns - lookback_minutes * 60 * 1_000_000_000
        payload = self._get(
            params={
                "query": LOKI_LOG_QUERY,
                "start": str(start_ns),
                "end": str(end_ns),
                "limit": MAX_LOG_ENTRIES,
                "direction": "backward",
            }
        )
        observed = self._validate_response(payload, start_ns=start_ns, end_ns=end_ns)
        return self._build_evidence(
            observed,
            service_name=service_name,
            lookback_minutes=lookback_minutes,
        )

    def query_metrics(
        self,
        *,
        service_name: str,
        lookback_minutes: int,
    ) -> Sequence[EvidenceObservation]:
        """Prometheus 由独立适配器负责，这里不伪造指标证据。"""

        self._validate_target(service_name)
        self._validate_minutes(lookback_minutes)
        return ()

    def list_recent_changes(
        self,
        *,
        service_name: str,
        lookback_hours: int,
    ) -> Sequence[EvidenceObservation]:
        """变更记录适配器尚未接入时不伪造变更证据。"""

        self._validate_target(service_name)
        if isinstance(lookback_hours, bool) or not isinstance(lookback_hours, int):
            raise ValueError("lookback_hours 必须是整数")
        if not 1 <= lookback_hours <= 24:
            raise ValueError("lookback_hours 必须在 1 到 24 之间")
        return ()

    def _get(self, *, params: dict[str, str | int]) -> object:
        try:
            with httpx2.Client(
                base_url=self._settings.loki_base_url,
                timeout=self._settings.request_timeout_seconds,
                transport=self._transport,
                trust_env=False,
            ) as client:
                response = client.get("/loki/api/v1/query_range", params=params)
        except httpx2.TimeoutException as error:
            raise InvestigationDataSourceFailure(
                error_code="loki_query_timeout",
                message="Loki 调查查询超时",
            ) from error
        except httpx2.HTTPError as error:
            raise InvestigationDataSourceFailure(
                error_code="loki_query_unavailable",
                message="Loki 调查查询不可用",
            ) from error
        if not 200 <= response.status_code < 300:
            raise InvestigationDataSourceFailure(
                error_code="loki_unexpected_status",
                message="Loki 调查查询返回了非预期状态",
                http_status=response.status_code,
            )
        try:
            return response.json()
        except ValueError as error:
            raise InvestigationDataSourceFailure(
                error_code="loki_invalid_response",
                message="Loki 调查查询返回了无效 JSON",
                http_status=response.status_code,
            ) from error

    @classmethod
    def _validate_response(
        cls,
        payload: object,
        *,
        start_ns: int,
        end_ns: int,
    ) -> tuple[_ObservedFailure, ...]:
        try:
            response = _LokiQueryResponse.model_validate(payload)
        except (ValueError, TypeError) as error:
            raise InvestigationDataSourceFailure(
                error_code="loki_invalid_response",
                message="Loki 日志区间查询响应无效",
            ) from error

        entry_count = sum(len(stream.values) for stream in response.data.result)
        if entry_count > MAX_LOG_ENTRIES:
            raise InvestigationDataSourceFailure(
                error_code="loki_log_limit_exceeded",
                message="Loki 返回的日志数量超过允许上限",
            )
        observed_by_identity: dict[tuple[int, str], _ObservedFailure] = {}
        for stream in response.data.result:
            if stream.stream != {"service": SUPPORTED_SERVICE, "source": "fault-lab"}:
                raise InvestigationDataSourceFailure(
                    error_code="loki_invalid_labels",
                    message="Loki 日志查询返回了不匹配的流标签",
                )
            for timestamp_text, line in stream.values:
                observed = cls._validate_entry(
                    timestamp_text,
                    line,
                    start_ns=start_ns,
                    end_ns=end_ns,
                )
                observed_by_identity[(observed.loki_timestamp_ns, observed.raw_line)] = observed
        return tuple(
            sorted(
                observed_by_identity.values(),
                key=lambda item: (item.event.timestamp, item.raw_line),
            )
        )

    @staticmethod
    def _validate_entry(
        timestamp_text: str,
        line: str,
        *,
        start_ns: int,
        end_ns: int,
    ) -> _ObservedFailure:
        if len(line.encode("utf-8")) > MAX_LOG_LINE_BYTES:
            raise InvestigationDataSourceFailure(
                error_code="loki_log_line_too_large",
                message="Loki 日志行超过允许大小",
            )
        try:
            timestamp_ns = int(timestamp_text)
        except ValueError as error:
            raise InvestigationDataSourceFailure(
                error_code="loki_invalid_timestamp",
                message="Loki 日志时间不是有效纳秒时间戳",
            ) from error
        if not start_ns <= timestamp_ns <= end_ns + int(MAX_FUTURE_CLOCK_SKEW_SECONDS * 1e9):
            raise InvestigationDataSourceFailure(
                error_code="loki_invalid_timestamp",
                message="Loki 日志时间不在请求窗口内",
            )
        try:
            payload = json.loads(line)
            event = _FAILURE_LOG_ADAPTER.validate_python(payload)
            return _ObservedFailure(
                loki_timestamp_ns=timestamp_ns,
                event=event,
                raw_line=line,
            )
        except (ValueError, TypeError) as error:
            raise InvestigationDataSourceFailure(
                error_code="loki_invalid_log_entry",
                message="Loki 日志不符合结构化事故事件契约",
            ) from error

    @classmethod
    def _build_evidence(
        cls,
        observed: tuple[_ObservedFailure, ...],
        *,
        service_name: str,
        lookback_minutes: int,
    ) -> tuple[EvidenceObservation, ...]:
        grouped: dict[str, list[_ObservedFailure]] = defaultdict(list)
        for item in observed:
            grouped[item.event.fault].append(item)

        evidence: list[EvidenceObservation] = []
        for fault in sorted(grouped):
            entries = grouped[fault]
            evidence_fault = fault.replace("_", "-")
            attributes = cls._build_attributes(
                entries,
                service_name=service_name,
                lookback_minutes=lookback_minutes,
            )
            observed_at = entries[-1].event.timestamp
            digest = hashlib.sha256(
                json.dumps(
                    attributes,
                    ensure_ascii=False,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()[:12]
            evidence.append(
                EvidenceObservation(
                    evidence_id=(
                        f"loki-order-service-{evidence_fault}-"
                        f"{int(observed_at.timestamp())}-{digest}"
                    ),
                    kind=EvidenceKind.LOG,
                    source="loki-fault-lab",
                    summary=cls._summary(fault=fault, count=len(entries)),
                    observed_at=observed_at,
                    attributes=attributes,
                )
            )
        return tuple(evidence)

    @staticmethod
    def _build_attributes(
        entries: list[_ObservedFailure],
        *,
        service_name: str,
        lookback_minutes: int,
    ) -> dict[str, JsonValue]:
        first = entries[0].event
        latest = entries[-1].event
        attributes: dict[str, JsonValue] = {
            "service_name": service_name,
            "lookback_minutes": lookback_minutes,
            "event_count": len(entries),
            "route": latest.route,
            "status_code": latest.status_code,
            "error_type": latest.error_type,
            "first_observed_at": first.timestamp.isoformat().replace("+00:00", "Z"),
            "latest_observed_at": latest.timestamp.isoformat().replace("+00:00", "Z"),
        }
        if isinstance(latest, _ReleaseFailureLog):
            attributes["release_version"] = latest.release_version
        elif isinstance(latest, _DatabasePoolFailureLog):
            attributes["database_pool_size"] = latest.database_pool_size
            attributes["database_pool_waiters"] = latest.database_pool_waiters
            attributes["database_pool_waiters_max"] = max(
                item.event.database_pool_waiters
                for item in entries
                if isinstance(item.event, _DatabasePoolFailureLog)
            )
        else:
            attributes["dependency"] = "redis"
        return attributes

    @staticmethod
    def _summary(*, fault: str, count: int) -> str:
        descriptions = {
            "release_500": "未处理运行时异常",
            "database_pool_timeout": "数据库连接池获取超时",
            "redis_unavailable": "Redis 连接不可用",
        }
        return f"order-service 在查询窗口内出现 {count} 条{descriptions[fault]}日志。"

    @staticmethod
    def _validate_target(service_name: str) -> None:
        if service_name != SUPPORTED_SERVICE:
            raise UnsupportedInvestigationTarget(service_name)

    @staticmethod
    def _validate_minutes(lookback_minutes: int) -> None:
        if isinstance(lookback_minutes, bool) or not isinstance(lookback_minutes, int):
            raise ValueError("lookback_minutes 必须是整数")
        if not 1 <= lookback_minutes <= 60:
            raise ValueError("lookback_minutes 必须在 1 到 60 之间")
