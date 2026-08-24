"""业务数据库环境配置。"""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_DATABASE_URL = (
    "postgresql+psycopg://opsswarm:local-development-only@127.0.0.1:15432/opsswarm"
)


class DatabaseSettings(BaseSettings):
    """只从显式环境变量读取可覆盖的数据库连接配置。"""

    model_config = SettingsConfigDict(env_prefix="OPSSWARM_", extra="ignore")

    database_url: str = Field(default=DEFAULT_DATABASE_URL, min_length=1)
