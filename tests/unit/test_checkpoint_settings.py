import pytest
from pydantic import ValidationError

from opsswarm.checkpoints.settings import (
    CheckpointSettings,
    ensure_separate_checkpoint_database,
)


def test_checkpoint_settings_use_independent_database_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkpoint_url = "postgresql://checkpoint:test@127.0.0.1:5432/opsswarm_checkpoints"
    monkeypatch.setenv("OPSSWARM_CHECKPOINT_DATABASE_URL", checkpoint_url)

    settings = CheckpointSettings()

    assert settings.checkpoint_database_url == checkpoint_url


def test_checkpoint_settings_reject_sqlalchemy_driver_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "OPSSWARM_CHECKPOINT_DATABASE_URL",
        "postgresql+psycopg://checkpoint:test@127.0.0.1:5432/opsswarm_checkpoints",
    )

    with pytest.raises(ValidationError, match="必须使用 PostgreSQL URL"):
        CheckpointSettings()


def test_checkpoint_schema_cannot_be_initialized_in_business_database() -> None:
    with pytest.raises(ValueError, match="不能与业务数据库相同"):
        ensure_separate_checkpoint_database(
            "postgresql://checkpoint:test@localhost:5432/opsswarm",
            "postgresql+psycopg://business:test@127.0.0.1/opsswarm",
        )
