"""调查 Worker 数据来源能力配置测试。"""

import pytest
from pydantic import ValidationError

from opsswarm.investigation.models import InvestigationDataSourceMode
from opsswarm.worker.settings import InvestigationWorkerSettings


def test_worker_defaults_to_frozen_data(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPSSWARM_WORKER_DATA_SOURCE_MODE", raising=False)

    settings = InvestigationWorkerSettings()

    assert settings.data_source_mode is InvestigationDataSourceMode.FROZEN
    assert settings.poll_interval_seconds == 1
    assert settings.lease_duration_seconds == 120
    assert settings.processing_timeout_seconds == 100


def test_worker_live_mode_requires_exact_explicit_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPSSWARM_WORKER_DATA_SOURCE_MODE", "live")
    assert InvestigationWorkerSettings().data_source_mode is InvestigationDataSourceMode.LIVE

    monkeypatch.setenv("OPSSWARM_WORKER_DATA_SOURCE_MODE", "production")
    with pytest.raises(ValidationError):
        InvestigationWorkerSettings()


def test_worker_rejects_timeout_without_lease_settlement_margin() -> None:
    with pytest.raises(ValidationError, match="processing_timeout_seconds"):
        InvestigationWorkerSettings(
            lease_duration_seconds=30,
            processing_timeout_seconds=30,
        )


def test_worker_rejects_inverted_retry_delay_range() -> None:
    with pytest.raises(ValidationError, match="max_retry_delay_seconds"):
        InvestigationWorkerSettings(
            base_retry_delay_seconds=10,
            max_retry_delay_seconds=5,
        )
