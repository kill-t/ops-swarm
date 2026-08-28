"""可选真实模型的环境配置与工厂。"""

from collections.abc import Sequence
from typing import Any, Literal, Self, cast

from langchain_core.callbacks import BaseCallbackHandler
from langchain_openai import ChatOpenAI
from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ThinkingMode = Literal["provider_default", "enabled", "disabled"]
StructuredOutputMethod = Literal["function_calling", "json_mode", "json_schema"]


class LlmSettings(BaseSettings):
    """仅从 LLM_ 前缀环境变量读取 OpenAI 兼容接口配置。"""

    model_config = SettingsConfigDict(env_prefix="LLM_", extra="ignore")

    api_key: SecretStr = Field(default_factory=lambda: SecretStr(""))
    model: str = ""
    base_url: str | None = None
    temperature: float = Field(default=0, ge=0, le=2)
    thinking_mode: ThinkingMode = "provider_default"
    structured_output_method: StructuredOutputMethod = "json_schema"

    @model_validator(mode="after")
    def ensure_real_model_is_explicit(self) -> Self:
        """没有显式密钥和模型名时禁止误发真实模型请求。"""

        if not self.api_key.get_secret_value():
            raise ValueError("必须设置 LLM_API_KEY")
        if not self.model.strip():
            raise ValueError("必须设置 LLM_MODEL")
        return self

    @classmethod
    def from_dotenv(cls) -> Self:
        """仅供显式本地命令读取项目根目录的 .env。"""

        constructor = cast(Any, cls)
        return cast(Self, constructor(_env_file=".env", _env_file_encoding="utf-8"))


def create_openai_compatible_model(settings: LlmSettings) -> ChatOpenAI:
    """创建模型适配器但不发起网络请求。"""

    return ChatOpenAI(
        api_key=settings.api_key,
        model=settings.model,
        base_url=settings.base_url,
        temperature=settings.temperature,
        extra_body=_thinking_extra_body(settings),
    )


def create_bounded_openai_compatible_model(
    settings: LlmSettings,
    *,
    max_output_tokens: int,
    timeout_seconds: float,
    callbacks: Sequence[BaseCallbackHandler] = (),
) -> ChatOpenAI:
    """为显式评测创建带单请求输出上限、超时和审计回调的模型。"""

    return ChatOpenAI(
        api_key=settings.api_key,
        model=settings.model,
        base_url=settings.base_url,
        temperature=settings.temperature,
        max_completion_tokens=max_output_tokens,
        timeout=timeout_seconds,
        callbacks=list(callbacks),
        extra_body=_thinking_extra_body(settings),
    )


def _thinking_extra_body(settings: LlmSettings) -> dict[str, object] | None:
    """只在显式配置时向兼容接口传递思考模式，避免猜测供应商能力。"""

    if settings.thinking_mode == "provider_default":
        return None
    return {"thinking": {"type": settings.thinking_mode}}
