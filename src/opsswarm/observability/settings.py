"""只允许本地故障演练链路使用的 OpenTelemetry 配置。"""

from urllib.parse import urlparse

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_TRACES_ENDPOINT = "http://127.0.0.1:14318/v1/traces"
_ALLOWED_ENDPOINTS = frozenset(
    {
        ("127.0.0.1", 14318),
        ("localhost", 14318),
        ("127.0.0.1", 4318),
        ("localhost", 4318),
        ("jaeger", 4318),
    }
)


class TracingSettings(BaseSettings):
    """追踪默认关闭，显式开启后也只能发送到本地 Jaeger。"""

    model_config = SettingsConfigDict(
        env_prefix="OPSSWARM_OTEL_",
        extra="ignore",
    )

    enabled: bool = False
    traces_endpoint: str = Field(default=DEFAULT_TRACES_ENDPOINT, min_length=1)
    sample_ratio: float = Field(default=1.0, ge=0.0, le=1.0)
    export_timeout_seconds: float = Field(default=2.0, gt=0.0, le=10.0)

    @field_validator("traces_endpoint")
    @classmethod
    def restrict_traces_endpoint(cls, value: str) -> str:
        """MVP 不允许把事故元数据静默发送到外部遥测平台。"""

        normalized = value.strip().rstrip("/")
        parsed = urlparse(normalized)
        if (
            parsed.scheme != "http"
            or parsed.hostname is None
            or (parsed.hostname, parsed.port) not in _ALLOWED_ENDPOINTS
            or parsed.path != "/v1/traces"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.params
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "追踪地址只允许本机 14318/4318 或 Compose 内 jaeger:4318 的 /v1/traces"
            )
        return normalized
