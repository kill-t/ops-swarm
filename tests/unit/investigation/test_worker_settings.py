"""调查 Worker 数据来源能力配置测试。"""

import pytest
from pydantic import ValidationError

from opsswarm.investigation.models import InvestigationDataSourceMode
from opsswarm.worker.settings import InvestigationWorkerSettings


def test_worker_defaults_to_frozen_data(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPSSWARM_WORKER_DATA_SOURCE_MODE", raising=False)

    settings = InvestigationWorkerSettings()

    assert settings.data_source_mode is InvestigationDataSourceMode.FROZEN


def test_worker_live_mode_requires_exact_explicit_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPSSWARM_WORKER_DATA_SOURCE_MODE", "live")
    assert InvestigationWorkerSettings().data_source_mode is InvestigationDataSourceMode.LIVE

    monkeypatch.setenv("OPSSWARM_WORKER_DATA_SOURCE_MODE", "production")
    with pytest.raises(ValidationError):
        InvestigationWorkerSettings()
