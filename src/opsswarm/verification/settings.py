"""本地恢复探针配置。"""

from typing import Self
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_FAULT_LAB_BASE_URL = "http://127.0.0.1:18001"
ALLOWED_FAULT_LAB_HOSTS = frozenset({"127.0.0.1", "localhost", "order-service"})


class VerificationSettings(BaseSettings):
    """限制恢复探针只访问本地或 Compose 演练服务。"""

    model_config = SettingsConfigDict(env_prefix="OPSSWARM_VERIFIER_", extra="ignore")

    fault_lab_base_url: str = Field(default=DEFAULT_FAULT_LAB_BASE_URL, min_length=1)
    request_timeout_seconds: float = Field(default=3.0, gt=0, le=10)

    @field_validator("fault_lab_base_url")
    @classmethod
    def restrict_fault_lab_url(cls, value: str) -> str:
        normalized = value.rstrip("/")
        parsed = urlsplit(normalized)
        if parsed.scheme != "http":
            raise ValueError("恢复探针地址只允许 http")
        if parsed.hostname not in ALLOWED_FAULT_LAB_HOSTS:
            raise ValueError("恢复探针地址只允许 localhost、127.0.0.1 或 order-service")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("恢复探针地址不能包含用户信息")
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ValueError("恢复探针地址不能包含路径、查询参数或片段")
        return normalized


class VerificationWorkerSettings(BaseSettings):
    """恢复验证调度 Worker 的有限轮询、租约和退避参数。"""

    model_config = SettingsConfigDict(
        env_prefix="OPSSWARM_VERIFICATION_WORKER_",
        extra="ignore",
    )

    poll_interval_seconds: float = Field(default=1.0, ge=0.1, le=60)
    lease_duration_seconds: int = Field(default=15, ge=1, le=300)
    base_retry_delay_seconds: int = Field(default=2, ge=0, le=3600)
    max_retry_delay_seconds: int = Field(default=60, ge=0, le=3600)

    @model_validator(mode="after")
    def validate_retry_range(self) -> Self:
        if self.max_retry_delay_seconds < self.base_retry_delay_seconds:
            raise ValueError("最大重试延迟不能小于基础重试延迟")
        return self
