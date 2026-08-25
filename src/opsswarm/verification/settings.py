"""本地恢复探针配置。"""

from urllib.parse import urlsplit

from pydantic import Field, field_validator
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
