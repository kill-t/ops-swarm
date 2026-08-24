"""LangGraph checkpoint 独立数据库配置。"""

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL, make_url

DEFAULT_CHECKPOINT_DATABASE_URL = (
    "postgresql://opsswarm:local-development-only@127.0.0.1:15432/opsswarm_checkpoints"
)


class CheckpointSettings(BaseSettings):
    """只读取 checkpoint 数据库连接，不复用业务库 URL。"""

    model_config = SettingsConfigDict(env_prefix="OPSSWARM_", extra="ignore")

    checkpoint_database_url: str = Field(
        default=DEFAULT_CHECKPOINT_DATABASE_URL,
        min_length=1,
    )

    @field_validator("checkpoint_database_url")
    @classmethod
    def require_native_postgresql_url(cls, value: str) -> str:
        """Psycopg checkpointer 使用原生 PostgreSQL URL，而不是 SQLAlchemy 方言 URL。"""

        normalized = value.strip()
        if not normalized.startswith(("postgresql://", "postgres://")):
            raise ValueError("checkpoint_database_url 必须使用 PostgreSQL URL")
        return normalized


def ensure_separate_checkpoint_database(
    checkpoint_database_url: str,
    business_database_url: str,
) -> None:
    """阻止初始化命令把 LangGraph 表误建到业务事实库。"""

    checkpoint_endpoint = _database_endpoint(make_url(checkpoint_database_url))
    business_endpoint = _database_endpoint(make_url(business_database_url))
    if checkpoint_endpoint == business_endpoint:
        raise ValueError("checkpoint 数据库不能与业务数据库相同")


def _database_endpoint(url: URL) -> tuple[str, int, str | None]:
    host = (url.host or "").lower()
    if host in {"localhost", "127.0.0.1", "::1"}:
        host = "local"
    return host, url.port or 5432, url.database
