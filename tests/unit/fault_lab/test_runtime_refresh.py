"""固定控制器地址的运行证明刷新测试。"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx2
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

from opsswarm.fault_lab.runtime_metadata import (
    RuntimeArtifactIdentity,
    RuntimeArtifactInstance,
    RuntimeMetadataSettings,
)
from opsswarm.fault_lab.runtime_proof import (
    RuntimeInventoryProofPayload,
    RuntimeInventoryProofVerificationError,
    RuntimeProofPublicKeyRing,
    encode_ed25519_public_key,
    issue_runtime_inventory_proof,
)
from opsswarm.fault_lab.runtime_refresh import (
    HttpRuntimeInventoryProofRefresher,
    RuntimeInventoryProofRefreshFailure,
)

NOW = datetime(2026, 9, 1, 9, tzinfo=UTC)
INSTANCE_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
IMAGE_REPOSITORY = "ghcr.io/kill-t/ops-swarm-order-service"
IMAGE_DIGEST = "sha256:" + "b" * 64


def _identity() -> RuntimeArtifactIdentity:
    return RuntimeArtifactIdentity(
        instance_id=INSTANCE_ID,
        image_repository=IMAGE_REPOSITORY,
        image_digest=IMAGE_DIGEST,
    )


def _envelope(private_key: Ed25519PrivateKey) -> str:
    payload = RuntimeInventoryProofPayload(
        proof_id=UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
        issued_at=NOW,
        expires_at=NOW + timedelta(minutes=2),
        instances=(
            RuntimeArtifactInstance(
                **_identity().model_dump(),
                started_at=NOW - timedelta(minutes=1),
            ),
        ),
    )
    return issue_runtime_inventory_proof(
        payload,
        key_id="local-controller-1",
        private_key=private_key,
    ).model_dump_json()


def _refresher(
    private_key: Ed25519PrivateKey,
    handler: httpx2.MockTransport,
) -> HttpRuntimeInventoryProofRefresher:
    return HttpRuntimeInventoryProofRefresher(
        base_url="http://deployment-controller:8000",
        timeout_seconds=1,
        public_keys=RuntimeProofPublicKeyRing(
            keys={"local-controller-1": encode_ed25519_public_key(private_key)}
        ),
        transport=handler,
        clock=lambda: NOW + timedelta(seconds=1),
    )


def test_refresh_uses_exact_read_only_endpoint_and_verifies_response() -> None:
    private_key = Ed25519PrivateKey.generate()
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(200, content=_envelope(private_key))

    verified = _refresher(private_key, httpx2.MockTransport(handler)).refresh(identity=_identity())

    assert verified.summary.key_id == "local-controller-1"
    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert str(requests[0].url) == (
        "http://deployment-controller:8000/api/v1/runtime-inventory-proof"
    )


@pytest.mark.parametrize(
    ("response", "expected_message"),
    [
        (httpx2.Response(503), "非预期状态"),
        (httpx2.Response(200, content=b"not-json"), "非法证明"),
        (httpx2.Response(200, content=b"x" * (64 * 1024 + 1)), "超过 64 KiB"),
    ],
)
def test_refresh_transport_failures_are_closed_without_fallback(
    response: httpx2.Response,
    expected_message: str,
) -> None:
    private_key = Ed25519PrivateKey.generate()
    refresher = _refresher(
        private_key,
        httpx2.MockTransport(lambda _: response),
    )

    with pytest.raises(RuntimeInventoryProofRefreshFailure) as captured:
        refresher.refresh(identity=_identity())

    assert captured.value.error_code == "runtime_inventory_proof_refresh_failed"
    assert expected_message in captured.value.message


def test_refresh_does_not_trust_a_valid_proof_from_an_unknown_signer() -> None:
    trusted_key = Ed25519PrivateKey.generate()
    unknown_key = Ed25519PrivateKey.generate()
    refresher = _refresher(
        trusted_key,
        httpx2.MockTransport(lambda _: httpx2.Response(200, content=_envelope(unknown_key))),
    )

    with pytest.raises(RuntimeInventoryProofVerificationError):
        refresher.refresh(identity=_identity())


@pytest.mark.parametrize(
    "base_url",
    [
        "https://deployment-controller:8000",
        "http://deployment-controller:8001",
        "http://attacker:8000",
        "http://127.0.0.1:18003/path",
        "http://user@127.0.0.1:18003",
    ],
)
def test_runtime_settings_reject_arbitrary_refresh_urls(base_url: str) -> None:
    private_key = Ed25519PrivateKey.generate()
    key_ring = RuntimeProofPublicKeyRing(
        keys={"local-controller-1": encode_ed25519_public_key(private_key)}
    )

    with pytest.raises(ValidationError):
        RuntimeMetadataSettings(
            metadata_enabled=True,
            instance_id=str(INSTANCE_ID),
            image_repository=IMAGE_REPOSITORY,
            image_digest=IMAGE_DIGEST,
            proof_refresh_base_url=base_url,
            proof_public_keys=key_ring.model_dump_json(),
        )


def test_static_and_dynamic_proof_sources_are_mutually_exclusive() -> None:
    private_key = Ed25519PrivateKey.generate()
    key_ring = RuntimeProofPublicKeyRing(
        keys={"local-controller-1": encode_ed25519_public_key(private_key)}
    )

    with pytest.raises(ValidationError):
        RuntimeMetadataSettings(
            metadata_enabled=True,
            instance_id=str(INSTANCE_ID),
            image_repository=IMAGE_REPOSITORY,
            image_digest=IMAGE_DIGEST,
            inventory_proof=_envelope(private_key),
            proof_refresh_base_url="http://deployment-controller:8000",
            proof_public_keys=key_ring.model_dump_json(),
        )
