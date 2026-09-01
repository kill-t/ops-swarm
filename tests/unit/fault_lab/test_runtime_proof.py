"""运行清单 Ed25519 证明的纯单元测试。"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

from opsswarm.fault_lab.runtime_metadata import (
    RuntimeArtifactIdentity,
    RuntimeArtifactInstance,
    RuntimeInventoryProofSummary,
)
from opsswarm.fault_lab.runtime_proof import (
    RuntimeInventoryProofEnvelope,
    RuntimeInventoryProofPayload,
    RuntimeInventoryProofVerificationError,
    RuntimeProofPublicKeyRing,
    bind_proof_to_runtime_identity,
    encode_ed25519_public_key,
    issue_runtime_inventory_proof,
    verify_runtime_inventory_proof,
)

NOW = datetime(2026, 9, 1, 8, tzinfo=UTC)
INSTANCE_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
IMAGE_REPOSITORY = "ghcr.io/kill-t/ops-swarm-order-service"
IMAGE_DIGEST = "sha256:" + "b" * 64


def _payload(**updates: object) -> RuntimeInventoryProofPayload:
    values: dict[str, object] = {
        "proof_id": UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
        "issued_at": NOW,
        "expires_at": NOW + timedelta(minutes=2),
        "instances": (
            RuntimeArtifactInstance(
                instance_id=INSTANCE_ID,
                image_repository=IMAGE_REPOSITORY,
                image_digest=IMAGE_DIGEST,
                started_at=NOW - timedelta(minutes=1),
            ),
        ),
    }
    values.update(updates)
    return RuntimeInventoryProofPayload.model_validate(values)


def _signed_proof() -> tuple[
    RuntimeInventoryProofEnvelope,
    RuntimeProofPublicKeyRing,
    Ed25519PrivateKey,
]:
    private_key = Ed25519PrivateKey.generate()
    envelope = issue_runtime_inventory_proof(
        _payload(),
        key_id="controller-2026-09",
        private_key=private_key,
    )
    key_ring = RuntimeProofPublicKeyRing(
        keys={"controller-2026-09": encode_ed25519_public_key(private_key)}
    )
    return envelope, key_ring, private_key


def test_valid_proof_is_verified_and_bound_to_current_runtime() -> None:
    envelope, key_ring, _ = _signed_proof()

    verified = verify_runtime_inventory_proof(
        envelope,
        key_ring,
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

    assert verified.summary.key_id == "controller-2026-09"
    assert verified.summary.algorithm == "Ed25519"
    assert verified.instances[0].instance_id == INSTANCE_ID


def test_tampered_payload_is_rejected() -> None:
    envelope, key_ring, _ = _signed_proof()
    tampered = envelope.model_copy(
        update={
            "payload": envelope.payload.model_copy(
                update={"proof_id": UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")}
            )
        }
    )

    with pytest.raises(RuntimeInventoryProofVerificationError) as captured:
        verify_runtime_inventory_proof(tampered, key_ring, current_time=NOW)

    assert captured.value.error_code == "runtime_inventory_proof_invalid"


def test_unknown_key_and_wrong_signature_are_rejected() -> None:
    envelope, _, _ = _signed_proof()
    other_private_key = Ed25519PrivateKey.generate()
    unknown_ring = RuntimeProofPublicKeyRing(
        keys={"other-key": encode_ed25519_public_key(other_private_key)}
    )
    wrong_ring = RuntimeProofPublicKeyRing(
        keys={"controller-2026-09": encode_ed25519_public_key(other_private_key)}
    )

    with pytest.raises(RuntimeInventoryProofVerificationError) as unknown:
        verify_runtime_inventory_proof(
            envelope,
            unknown_ring,
            current_time=NOW,
        )
    with pytest.raises(RuntimeInventoryProofVerificationError) as wrong:
        verify_runtime_inventory_proof(
            envelope,
            wrong_ring,
            current_time=NOW,
        )

    assert unknown.value.error_code == "runtime_inventory_proof_invalid"
    assert wrong.value.error_code == "runtime_inventory_proof_invalid"


@pytest.mark.parametrize(
    ("current_time", "error_code"),
    [
        (NOW - timedelta(microseconds=1), "runtime_inventory_proof_not_yet_valid"),
        (NOW + timedelta(minutes=2), "runtime_inventory_proof_expired"),
    ],
)
def test_proof_time_window_fails_closed(current_time: datetime, error_code: str) -> None:
    envelope, key_ring, _ = _signed_proof()

    with pytest.raises(RuntimeInventoryProofVerificationError) as captured:
        verify_runtime_inventory_proof(
            envelope,
            key_ring,
            current_time=current_time,
        )

    assert captured.value.error_code == error_code


def test_proof_lifetime_cannot_exceed_five_minutes() -> None:
    with pytest.raises(ValidationError):
        _payload(expires_at=NOW + timedelta(minutes=5, microseconds=1))
    with pytest.raises(ValidationError):
        RuntimeInventoryProofSummary(
            proof_id=UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
            key_id="controller-2026-09",
            issued_at=NOW,
            expires_at=NOW + timedelta(minutes=5, microseconds=1),
        )


def test_proof_must_bind_exact_current_instance_artifact() -> None:
    envelope, key_ring, _ = _signed_proof()
    verified = verify_runtime_inventory_proof(
        envelope,
        key_ring,
        current_time=NOW,
    )

    with pytest.raises(RuntimeInventoryProofVerificationError) as captured:
        bind_proof_to_runtime_identity(
            verified,
            RuntimeArtifactIdentity(
                instance_id=INSTANCE_ID,
                image_repository=IMAGE_REPOSITORY,
                image_digest="sha256:" + "c" * 64,
            ),
        )

    assert captured.value.error_code == "runtime_inventory_proof_invalid"
