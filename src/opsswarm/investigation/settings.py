"""可选真实模型的环境配置与工厂。"""

from typing import Self

from langchain_openai import ChatOpenAI
from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class LlmSettings(BaseSettings):
    """仅从 LLM_ 前缀环境变量读取 OpenAI 兼容接口配置。"""

    model_config = SettingsConfigDict(env_prefix="LLM_", extra="ignore")

    api_key: SecretStr = Field(default_factory=lambda: SecretStr(""))
    model: str = ""
    base_url: str | None = None
    temperature: float = Field(default=0, ge=0, le=2)

    @model_validator(mode="after")
    def ensure_real_model_is_explicit(self) -> Self:
        """没有显式密钥和模型名时禁止误发真实模型请求。"""

        if not self.api_key.get_secret_value():
            raise ValueError("必须设置 LLM_API_KEY")
        if not self.model.strip():
            raise ValueError("必须设置 LLM_MODEL")
        return self


def create_openai_compatible_model(settings: LlmSettings) -> ChatOpenAI:
    """创建模型适配器但不发起网络请求。"""

    return ChatOpenAI(
        api_key=settings.api_key,
        model=settings.model,
        base_url=settings.base_url,
        temperature=settings.temperature,
    )
