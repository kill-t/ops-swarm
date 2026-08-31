"""受限 CI/CD 部署事件配置与数据契约测试。"""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from opsswarm.change_records.deployment_ingestion import (
    DeploymentEvent,
    DeploymentIngestionSettings,
)

SECRET = "s" * 32
COMMIT_SHA = "a" * 40
IMAGE_REPOSITORY = "ghcr.io/kill-t/ops-swarm-order-service"
IMAGE_DIGEST = "sha256:" + "b" * 64


def test_ingestion_is_safe_off_by_default() -> None:
    settings = DeploymentIngestionSettings()

    assert settings.enabled is False
    assert settings.key_id == ""
    assert settings.secret.get_secret_value() == ""


def test_enabled_ingestion_requires_complete_single_identity_and_repository() -> None:
    settings = DeploymentIngestionSettings(
        enabled=True,
        key_id="github-actions-production",
        secret=SECRET,
        repository="kill-t/ops-swarm",
        image_repository=IMAGE_REPOSITORY,
    )

    assert settings.key_id == "github-actions-production"
    assert settings.repository == "kill-t/ops-swarm"
    assert settings.image_repository == IMAGE_REPOSITORY

    invalid_configurations = (
        {
            "key_id": "local-admin",
            "secret": SECRET,
            "repository": "kill-t/ops-swarm",
            "image_repository": IMAGE_REPOSITORY,
        },
        {
            "key_id": "github-actions-production",
            "secret": "too-short",
            "repository": "kill-t/ops-swarm",
            "image_repository": IMAGE_REPOSITORY,
        },
        {
            "key_id": "github-actions-production",
            "secret": SECRET,
            "repository": "kill-t/..",
            "image_repository": IMAGE_REPOSITORY,
        },
        {
            "key_id": "github-actions-production",
            "secret": SECRET,
            "repository": "kill-t/ops-swarm",
            "image_repository": "ghcr.io/kill-t/order-service:latest",
        },
    )
    for configuration in invalid_configurations:
        with pytest.raises(ValidationError):
            DeploymentIngestionSettings(enabled=True, **configuration)  # type: ignore[arg-type]


def test_deployment_event_normalizes_time_and_rejects_open_ended_input() -> None:
    event = DeploymentEvent(
        delivery_id="delivery-1",
        environment="fault-lab",
        service_name="order-service",
        repository="kill-t/ops-swarm",
        commit_sha=COMMIT_SHA,
        image_repository=IMAGE_REPOSITORY,
        image_digest=IMAGE_DIGEST,
        from_version="stable",
        to_version="rc1",
        deployment_status="succeeded",
        deployed_at="2026-08-30T20:00:00+08:00",  # type: ignore[arg-type]
    )

    assert event.deployed_at == datetime(2026, 8, 30, 12, tzinfo=UTC)

    payload = event.model_dump(mode="json")
    payload["unknown"] = "not-allowed"
    with pytest.raises(ValidationError):
        DeploymentEvent.model_validate(payload)

    payload.pop("unknown")
    payload["to_version"] = "stable"
    with pytest.raises(ValidationError):
        DeploymentEvent.model_validate(payload)

    payload["to_version"] = "rc1"
    payload["deployed_at"] = "2026-08-30T12:00:00"
    with pytest.raises(ValidationError):
        DeploymentEvent.model_validate(payload)

    payload["deployed_at"] = "2026-08-30T12:00:00Z"
    payload["image_digest"] = "sha256:not-a-digest"
    with pytest.raises(ValidationError):
        DeploymentEvent.model_validate(payload)
