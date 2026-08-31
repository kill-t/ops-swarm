"""调查 Worker 的显式运行能力配置。"""

from typing import Any, Self, cast

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from opsswarm.investigation.models import InvestigationDataSourceMode


class InvestigationWorkerSettings(BaseSettings):
    """限制一个 Worker 进程只领取一种持久化数据来源模式。"""

    model_config = SettingsConfigDict(env_prefix="OPSSWARM_WORKER_", extra="ignore")

    data_source_mode: InvestigationDataSourceMode = InvestigationDataSourceMode.FROZEN
    poll_interval_seconds: float = Field(default=1.0, ge=0.1, le=60.0)
    lease_duration_seconds: float = Field(default=120.0, ge=10.0, le=1800.0)
    processing_timeout_seconds: float = Field(default=100.0, ge=1.0, le=1790.0)
    base_retry_delay_seconds: float = Field(default=5.0, ge=0.0, le=3600.0)
    max_retry_delay_seconds: float = Field(default=300.0, ge=0.0, le=3600.0)

    @model_validator(mode="after")
    def validate_timing_relationships(self) -> Self:
        """处理超时必须给租约结算留出余量，退避上限不能小于起点。"""

        if self.processing_timeout_seconds >= self.lease_duration_seconds:
            raise ValueError("processing_timeout_seconds 必须小于 lease_duration_seconds")
        if self.max_retry_delay_seconds < self.base_retry_delay_seconds:
            raise ValueError("max_retry_delay_seconds 不能小于 base_retry_delay_seconds")
        return self

    @classmethod
    def from_dotenv(cls) -> Self:
        """仅供显式本地命令读取项目根目录的 .env。"""

        constructor = cast(Any, cls)
        return cast(Self, constructor(_env_file=".env", _env_file_encoding="utf-8"))
