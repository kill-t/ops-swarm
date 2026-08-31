import pytest
from pydantic import ValidationError

from opsswarm.investigation.settings import (
    LlmSettings,
    create_bounded_openai_compatible_model,
    create_openai_compatible_model,
)


def test_llm_settings_require_explicit_secret_and_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)

    with pytest.raises(ValidationError, match="必须设置 LLM_API_KEY"):
        LlmSettings()


def test_openai_compatible_model_is_created_without_network_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_API_KEY", "test-only-key")
    monkeypatch.setenv("LLM_MODEL", "test-model")
    monkeypatch.setenv("LLM_BASE_URL", "http://127.0.0.1:9999/v1")

    settings = LlmSettings()
    model = create_openai_compatible_model(settings)

    assert settings.api_key.get_secret_value() == "test-only-key"
    assert model.model_name == "test-model"
    assert str(model.openai_api_base) == "http://127.0.0.1:9999/v1"
    assert model.temperature == 0


def test_blank_base_url_is_treated_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Compose 传入空字符串时仍使用供应商默认地址。"""

    monkeypatch.setenv("LLM_API_KEY", "test-only-key")
    monkeypatch.setenv("LLM_MODEL", "test-model")
    monkeypatch.setenv("LLM_BASE_URL", "   ")

    settings = LlmSettings()

    assert settings.base_url is None


def test_model_factory_applies_explicit_provider_compatibility(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_API_KEY", "test-only-key")
    monkeypatch.setenv("LLM_MODEL", "deepseek-v4-flash")
    monkeypatch.setenv("LLM_THINKING_MODE", "disabled")
    monkeypatch.setenv("LLM_STRUCTURED_OUTPUT_METHOD", "json_mode")

    settings = LlmSettings()
    model = create_openai_compatible_model(settings)
    bounded_model = create_bounded_openai_compatible_model(
        settings,
        max_output_tokens=128,
        timeout_seconds=5,
    )

    assert settings.structured_output_method == "json_mode"
    assert model.extra_body == {"thinking": {"type": "disabled"}}
    assert bounded_model.extra_body == {"thinking": {"type": "disabled"}}
