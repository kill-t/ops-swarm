"""调查 Worker 的显式运行能力配置。"""

from typing import Any, Self, cast

from pydantic_settings import BaseSettings, SettingsConfigDict

from opsswarm.investigation.models import InvestigationDataSourceMode


class InvestigationWorkerSettings(BaseSettings):
    """限制一个 Worker 进程只领取一种持久化数据来源模式。"""

    model_config = SettingsConfigDict(env_prefix="OPSSWARM_WORKER_", extra="ignore")

    data_source_mode: InvestigationDataSourceMode = InvestigationDataSourceMode.FROZEN

    @classmethod
    def from_dotenv(cls) -> Self:
        """仅供显式本地命令读取项目根目录的 .env。"""

        constructor = cast(Any, cls)
        return cast(Self, constructor(_env_file=".env", _env_file_encoding="utf-8"))
