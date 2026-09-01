"""独立部署控制器 API 测试。"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from pydantic import ValidationError

from opsswarm.deployment_controller.app import create_app
from opsswarm.deployment_controller.settings import DeploymentControllerSettings
from opsswarm.fault_lab.runtime_metadata import RuntimeArtifactIdentity
from opsswarm.fault_lab.runtime_proof import (
    RuntimeInventoryProofEnvelope,
    RuntimeProofPublicKeyRing,
    bind_proof_to_runtime_identity,
    encode_ed25519_private_key,
    encode_ed25519_public_key,
    verify_runtime_inventory_proof,
)

NOW = datetime(2026, 9, 1, 9, tzinfo=UTC)
INSTANCE_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
IMAGE_REPOSITORY = "ghcr.io/kill-t/ops-swarm-order-service"
IMAGE_DIGEST = "sha256:" + "b" * 64


def _settings(private_key: Ed25519PrivateKey) -> DeploymentControllerSettings:
    return DeploymentControllerSettings(
        private_key=encode_ed25519_private_key(private_key),
        key_id="local-controller-2026-09",
        instance_id=str(INSTANCE_ID),
        image_repository=IMAGE_REPOSITORY,
        image_digest=IMAGE_DIGEST,
        proof_lifetime_seconds=120,
    )


def test_controller_issues_fresh_verifiable_proof_without_key_material() -> None:
    private_key = Ed25519PrivateKey.generate()
    proof_ids = iter(
        (
            UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
            UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc"),
        )
    )
    app = create_app(
        _settings(private_key),
        clock=lambda: NOW,
        proof_id_factory=lambda: next(proof_ids),
    )

    with TestClient(app) as client:
        health = client.get("/health")
        first = client.get("/api/v1/runtime-inventory-proof")
        second = client.get("/api/v1/runtime-inventory-proof")

    assert health.json() == {"service": "deployment-controller", "status": "healthy"}
    assert first.status_code == 200
    assert first.headers["cache-control"] == "no-store"
    assert first.json()["payload"]["proof_id"] != second.json()["payload"]["proof_id"]
    assert "private_key" not in first.text
    assert "public_key" not in first.text

    envelope = RuntimeInventoryProofEnvelope.model_validate(first.json())
    verified = verify_runtime_inventory_proof(
        envelope,
        RuntimeProofPublicKeyRing(
            keys={
                "local-controller-2026-09": encode_ed25519_public_key(private_key),
            }
        ),
        current_time=NOW + timedelta(seconds=1),
    )
    bind_proof_to_runtime_identity(
        verified,
        RuntimeArtifactIdentity(
            instance_id=INSTANCE_ID,
            image_repository=IMAGE_REPOSITORY,
            image_digest=IMAGE_DIGEST,
        ),
    )
    assert verified.envelope.payload.expires_at == NOW + timedelta(seconds=120)


@pytest.mark.parametrize(
    "values",
    [
        {},
        {"private_key": "not-a-key"},
        {"proof_lifetime_seconds": 301},
    ],
)
def test_controller_rejects_missing_or_invalid_signing_configuration(
    values: dict[str, object],
) -> None:
    private_key = Ed25519PrivateKey.generate()
    base: dict[str, object] = {
        "private_key": encode_ed25519_private_key(private_key),
        "instance_id": str(INSTANCE_ID),
        "image_repository": IMAGE_REPOSITORY,
        "image_digest": IMAGE_DIGEST,
    }
    base.update(values)
    if not values:
        base["private_key"] = ""

    with pytest.raises((ValidationError, ValueError)):
        DeploymentControllerSettings.model_validate(base)


def test_controller_settings_hide_private_key_in_representation() -> None:
    private_key = Ed25519PrivateKey.generate()
    encoded_private_key = encode_ed25519_private_key(private_key)

    settings = _settings(private_key)

    assert encoded_private_key not in repr(settings)
