"""故障演练服务 API 测试。"""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from pydantic import ValidationError

from opsswarm.fault_lab.app import create_app
from opsswarm.fault_lab.controller import FaultController
from opsswarm.fault_lab.runtime_metadata import (
    RuntimeArtifactIdentity,
    RuntimeArtifactInstance,
    RuntimeInventoryDeclaration,
    RuntimeMetadataSettings,
    RuntimeRolloutContext,
)
from opsswarm.fault_lab.runtime_proof import (
    RuntimeInventoryProofPayload,
    RuntimeProofPublicKeyRing,
    VerifiedRuntimeInventoryProof,
    encode_ed25519_public_key,
    issue_runtime_inventory_proof,
    verify_runtime_inventory_proof,
)
from opsswarm.fault_lab.runtime_refresh import RuntimeInventoryProofRefreshFailure

NOW = datetime(2026, 8, 31, 12, tzinfo=UTC)
INSTANCE_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
IMAGE_DIGEST = "sha256:" + "b" * 64


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(create_app(FaultController())) as test_client:
        yield test_client


def test_health_endpoint(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"service": "order-service", "status": "healthy"}


def test_runtime_metadata_is_disabled_by_default(client: TestClient) -> None:
    response = client.get("/api/v1/runtime-metadata")
    inventory_response = client.get("/api/v1/runtime-inventory")

    assert response.status_code == 503
    assert response.json() == {
        "error_code": "runtime_metadata_unavailable",
        "message": "运行元数据未启用",
    }
    assert inventory_response.status_code == 503
    assert inventory_response.json() == {
        "error_code": "runtime_inventory_unavailable",
        "message": "运行清单未启用",
    }


def test_runtime_metadata_reports_complete_startup_identity() -> None:
    settings = RuntimeMetadataSettings(
        metadata_enabled=True,
        instance_id=str(INSTANCE_ID),
        image_repository="ghcr.io/kill-t/ops-swarm-order-service",
        image_digest=IMAGE_DIGEST,
    )
    with TestClient(
        create_app(
            FaultController(),
            runtime_metadata_settings=settings,
            runtime_clock=lambda: NOW,
        )
    ) as client:
        response = client.get("/api/v1/runtime-metadata")
        inventory_response = client.get("/api/v1/runtime-inventory")

    assert response.status_code == 200
    assert response.json() == {
        "schema_version": 1,
        "service_name": "order-service",
        "identity_source": "deployment-environment",
        "instance_id": str(INSTANCE_ID),
        "image_repository": "ghcr.io/kill-t/ops-swarm-order-service",
        "image_digest": IMAGE_DIGEST,
        "started_at": "2026-08-31T12:00:00Z",
        "reported_at": "2026-08-31T12:00:00Z",
    }
    assert inventory_response.status_code == 200
    assert inventory_response.json() == {
        "schema_version": 1,
        "service_name": "order-service",
        "identity_source": "deployment-environment",
        "instances": [
            {
                "instance_id": str(INSTANCE_ID),
                "image_repository": "ghcr.io/kill-t/ops-swarm-order-service",
                "image_digest": IMAGE_DIGEST,
                "started_at": "2026-08-31T12:00:00Z",
            }
        ],
        "rollout": {
            "phase": "stable",
            "rollout_id": None,
            "started_at": None,
            "deadline_at": None,
            "target_image_repository": None,
            "target_image_digest": None,
        },
        "reported_at": "2026-08-31T12:00:00Z",
    }


def test_runtime_inventory_reports_additional_instance_and_rollout_window() -> None:
    previous_digest = "sha256:" + "c" * 64
    declaration = RuntimeInventoryDeclaration(
        additional_instances=(
            RuntimeArtifactInstance(
                instance_id=UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
                image_repository="ghcr.io/kill-t/ops-swarm-order-service",
                image_digest=previous_digest,
                started_at=datetime(2026, 8, 31, 11, 30, tzinfo=UTC),
            ),
        ),
        rollout=RuntimeRolloutContext(
            phase="progressing",
            rollout_id=UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc"),
            started_at=datetime(2026, 8, 31, 11, 55, tzinfo=UTC),
            deadline_at=datetime(2026, 8, 31, 12, 5, tzinfo=UTC),
            target_image_repository="ghcr.io/kill-t/ops-swarm-order-service",
            target_image_digest=IMAGE_DIGEST,
        ),
    )
    settings = RuntimeMetadataSettings(
        metadata_enabled=True,
        instance_id=str(INSTANCE_ID),
        image_repository="ghcr.io/kill-t/ops-swarm-order-service",
        image_digest=IMAGE_DIGEST,
        inventory_declaration=declaration.model_dump_json(),
    )
    with TestClient(
        create_app(
            FaultController(),
            runtime_metadata_settings=settings,
            runtime_clock=lambda: NOW,
        )
    ) as client:
        response = client.get("/api/v1/runtime-inventory")

    body = response.json()
    assert response.status_code == 200
    assert len(body["instances"]) == 2
    assert body["instances"][1]["image_digest"] == previous_digest
    assert body["rollout"]["phase"] == "progressing"
    assert body["rollout"]["target_image_digest"] == IMAGE_DIGEST


def test_runtime_inventory_rejects_duplicate_current_instance() -> None:
    declaration = RuntimeInventoryDeclaration(
        additional_instances=(
            RuntimeArtifactInstance(
                instance_id=INSTANCE_ID,
                image_repository="ghcr.io/kill-t/ops-swarm-order-service",
                image_digest=IMAGE_DIGEST,
                started_at=NOW,
            ),
        )
    )

    with pytest.raises(ValidationError):
        RuntimeMetadataSettings(
            metadata_enabled=True,
            instance_id=str(INSTANCE_ID),
            image_repository="ghcr.io/kill-t/ops-swarm-order-service",
            image_digest=IMAGE_DIGEST,
            inventory_declaration=declaration.model_dump_json(),
        )


def test_runtime_inventory_rejects_instance_that_has_not_started() -> None:
    declaration = RuntimeInventoryDeclaration(
        additional_instances=(
            RuntimeArtifactInstance(
                instance_id=UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
                image_repository="ghcr.io/kill-t/ops-swarm-order-service",
                image_digest=IMAGE_DIGEST,
                started_at=datetime(2026, 8, 31, 12, 1, tzinfo=UTC),
            ),
        )
    )
    settings = RuntimeMetadataSettings(
        metadata_enabled=True,
        instance_id=str(INSTANCE_ID),
        image_repository="ghcr.io/kill-t/ops-swarm-order-service",
        image_digest=IMAGE_DIGEST,
        inventory_declaration=declaration.model_dump_json(),
    )

    with pytest.raises(ValidationError):
        create_app(
            FaultController(),
            runtime_metadata_settings=settings,
            runtime_clock=lambda: NOW,
        )


def test_runtime_inventory_rejects_rollout_window_over_thirty_minutes() -> None:
    with pytest.raises(ValidationError):
        RuntimeRolloutContext(
            phase="progressing",
            rollout_id=UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc"),
            started_at=NOW,
            deadline_at=datetime(2026, 8, 31, 12, 31, tzinfo=UTC),
            target_image_repository="ghcr.io/kill-t/ops-swarm-order-service",
            target_image_digest=IMAGE_DIGEST,
        )


def test_runtime_inventory_reports_verified_ed25519_proof() -> None:
    private_key = Ed25519PrivateKey.generate()
    payload = RuntimeInventoryProofPayload(
        proof_id=UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd"),
        issued_at=NOW - timedelta(seconds=1),
        expires_at=NOW + timedelta(minutes=2),
        instances=(
            RuntimeArtifactInstance(
                instance_id=INSTANCE_ID,
                image_repository="ghcr.io/kill-t/ops-swarm-order-service",
                image_digest=IMAGE_DIGEST,
                started_at=NOW - timedelta(minutes=1),
            ),
        ),
    )
    envelope = issue_runtime_inventory_proof(
        payload,
        key_id="controller-2026-09",
        private_key=private_key,
    )
    key_ring = RuntimeProofPublicKeyRing(
        keys={"controller-2026-09": encode_ed25519_public_key(private_key)}
    )
    settings = RuntimeMetadataSettings(
        metadata_enabled=True,
        instance_id=str(INSTANCE_ID),
        image_repository="ghcr.io/kill-t/ops-swarm-order-service",
        image_digest=IMAGE_DIGEST,
        inventory_proof=envelope.model_dump_json(),
        proof_public_keys=key_ring.model_dump_json(),
    )

    with TestClient(
        create_app(
            FaultController(),
            runtime_metadata_settings=settings,
            runtime_clock=lambda: NOW,
        )
    ) as client:
        response = client.get("/api/v1/runtime-inventory")

    assert response.status_code == 200
    assert response.json()["identity_source"] == "deployment-controller-ed25519"
    assert response.json()["proof"] == {
        "proof_id": "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
        "key_id": "controller-2026-09",
        "algorithm": "Ed25519",
        "issued_at": "2026-08-31T11:59:59Z",
        "expires_at": "2026-08-31T12:02:00Z",
    }


def test_runtime_inventory_refreshes_a_new_proof_for_every_request() -> None:
    private_key = Ed25519PrivateKey.generate()
    key_ring = RuntimeProofPublicKeyRing(
        keys={"local-controller-1": encode_ed25519_public_key(private_key)}
    )
    proof_ids = iter(
        (
            UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd"),
            UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"),
        )
    )

    class FreshProofRefresher:
        def refresh(
            self,
            *,
            identity: RuntimeArtifactIdentity,
        ) -> VerifiedRuntimeInventoryProof:
            payload = RuntimeInventoryProofPayload(
                proof_id=next(proof_ids),
                issued_at=NOW,
                expires_at=NOW + timedelta(minutes=2),
                instances=(
                    RuntimeArtifactInstance(
                        **identity.model_dump(),
                        started_at=NOW - timedelta(minutes=1),
                    ),
                ),
            )
            envelope = issue_runtime_inventory_proof(
                payload,
                key_id="local-controller-1",
                private_key=private_key,
            )
            return verify_runtime_inventory_proof(
                envelope,
                key_ring,
                current_time=NOW,
            )

    settings = RuntimeMetadataSettings(
        metadata_enabled=True,
        instance_id=str(INSTANCE_ID),
        image_repository="ghcr.io/kill-t/ops-swarm-order-service",
        image_digest=IMAGE_DIGEST,
        proof_refresh_base_url="http://deployment-controller:8000",
        proof_public_keys=key_ring.model_dump_json(),
    )
    with TestClient(
        create_app(
            FaultController(),
            runtime_metadata_settings=settings,
            runtime_proof_refresher=FreshProofRefresher(),
            runtime_clock=lambda: NOW,
        )
    ) as client:
        first = client.get("/api/v1/runtime-inventory")
        second = client.get("/api/v1/runtime-inventory")

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["proof"]["proof_id"] != second.json()["proof"]["proof_id"]
    assert first.json()["identity_source"] == "deployment-controller-ed25519"


def test_runtime_inventory_refresh_failure_does_not_fallback_to_old_data() -> None:
    private_key = Ed25519PrivateKey.generate()
    key_ring = RuntimeProofPublicKeyRing(
        keys={"local-controller-1": encode_ed25519_public_key(private_key)}
    )

    class FailingProofRefresher:
        def refresh(
            self,
            *,
            identity: RuntimeArtifactIdentity,
        ) -> VerifiedRuntimeInventoryProof:
            raise RuntimeInventoryProofRefreshFailure()

    settings = RuntimeMetadataSettings(
        metadata_enabled=True,
        instance_id=str(INSTANCE_ID),
        image_repository="ghcr.io/kill-t/ops-swarm-order-service",
        image_digest=IMAGE_DIGEST,
        proof_refresh_base_url="http://deployment-controller:8000",
        proof_public_keys=key_ring.model_dump_json(),
    )
    with TestClient(
        create_app(
            FaultController(),
            runtime_metadata_settings=settings,
            runtime_proof_refresher=FailingProofRefresher(),
            runtime_clock=lambda: NOW,
        )
    ) as client:
        response = client.get("/api/v1/runtime-inventory")

    assert response.status_code == 503
    assert response.json() == {
        "error_code": "runtime_inventory_proof_refresh_failed",
        "message": "运行证明动态刷新失败",
    }


def test_expired_runtime_inventory_proof_is_not_served() -> None:
    private_key = Ed25519PrivateKey.generate()
    current_time = [NOW]
    payload = RuntimeInventoryProofPayload(
        proof_id=UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd"),
        issued_at=NOW - timedelta(seconds=1),
        expires_at=NOW + timedelta(seconds=1),
        instances=(
            RuntimeArtifactInstance(
                instance_id=INSTANCE_ID,
                image_repository="ghcr.io/kill-t/ops-swarm-order-service",
                image_digest=IMAGE_DIGEST,
                started_at=NOW - timedelta(minutes=1),
            ),
        ),
    )
    envelope = issue_runtime_inventory_proof(
        payload,
        key_id="controller-2026-09",
        private_key=private_key,
    )
    key_ring = RuntimeProofPublicKeyRing(
        keys={"controller-2026-09": encode_ed25519_public_key(private_key)}
    )
    app = create_app(
        FaultController(),
        runtime_metadata_settings=RuntimeMetadataSettings(
            metadata_enabled=True,
            instance_id=str(INSTANCE_ID),
            image_repository="ghcr.io/kill-t/ops-swarm-order-service",
            image_digest=IMAGE_DIGEST,
            inventory_proof=envelope.model_dump_json(),
            proof_public_keys=key_ring.model_dump_json(),
        ),
        runtime_clock=lambda: current_time[0],
    )

    with TestClient(app) as client:
        current_time[0] = NOW + timedelta(seconds=1)
        response = client.get("/api/v1/runtime-inventory")

    assert response.status_code == 503
    assert response.json() == {
        "error_code": "runtime_inventory_proof_expired",
        "message": "运行证明已经过期",
    }


def test_runtime_proof_and_unsigned_declaration_are_mutually_exclusive() -> None:
    with pytest.raises(ValidationError):
        RuntimeMetadataSettings(
            metadata_enabled=True,
            instance_id=str(INSTANCE_ID),
            image_repository="ghcr.io/kill-t/ops-swarm-order-service",
            image_digest=IMAGE_DIGEST,
            inventory_declaration="{}",
            inventory_proof="{}",
            proof_public_keys="{}",
        )


@pytest.mark.parametrize(
    ("instance_id", "image_repository", "image_digest"),
    [
        ("", "ghcr.io/kill-t/ops-swarm-order-service", IMAGE_DIGEST),
        (str(INSTANCE_ID), "repository:latest", IMAGE_DIGEST),
        (str(INSTANCE_ID), "ghcr.io/kill-t/ops-swarm-order-service", "sha256:bad"),
    ],
)
def test_enabled_runtime_metadata_rejects_incomplete_or_unsafe_identity(
    instance_id: str,
    image_repository: str,
    image_digest: str,
) -> None:
    with pytest.raises(ValidationError):
        RuntimeMetadataSettings(
            metadata_enabled=True,
            instance_id=instance_id,
            image_repository=image_repository,
            image_digest=image_digest,
        )


def test_metrics_endpoint_exposes_live_fault_state(client: TestClient) -> None:
    client.put("/__admin/dependencies/redis", json={"available": False})

    response = client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert (
        'opsswarm_fault_lab_redis_unavailable_ratio{service="order-service"} 1.0' in response.text
    )
    assert 'opsswarm_fault_lab_http_5xx_ratio{service="order-service"} 1.0' in response.text


def test_order_endpoint_is_healthy_by_default(client: TestClient) -> None:
    response = client.get("/orders/order-001")

    assert response.status_code == 200
    assert response.json() == {
        "order_id": "order-001",
        "status": "confirmed",
        "amount": "99.00",
        "currency": "CNY",
    }


def test_release_500_fault_can_be_enabled_and_disabled(client: TestClient) -> None:
    enabled = client.put("/__admin/faults/release-500", json={"enabled": True})
    failed_order = client.get("/orders/order-001")
    disabled = client.put("/__admin/faults/release-500", json={"enabled": False})
    recovered_order = client.get("/orders/order-001")

    assert enabled.status_code == 200
    assert enabled.json() == {"name": "release_500", "enabled": True}
    assert failed_order.status_code == 500
    assert failed_order.json() == {
        "error_code": "release_500",
        "message": "新版本故障注入已启用，订单接口模拟返回 500",
        "order_id": "order-001",
    }
    assert disabled.json() == {"name": "release_500", "enabled": False}
    assert recovered_order.status_code == 200


def test_fault_status_can_be_queried(client: TestClient) -> None:
    response = client.get("/__admin/faults/release-500")

    assert response.status_code == 200
    assert response.json() == {"name": "release_500", "enabled": False}


def test_database_pool_configuration_is_healthy_by_default(client: TestClient) -> None:
    response = client.get("/__admin/configuration/database-pool")

    assert response.status_code == 200
    assert response.json() == {
        "key": "database_pool_size",
        "value": 50,
        "database_pool_saturation": 0.2,
        "database_pool_waiters": 0,
        "status": "healthy",
    }


def test_redis_dependency_is_healthy_by_default(client: TestClient) -> None:
    response = client.get("/__admin/dependencies/redis")

    assert response.status_code == 200
    assert response.json() == {
        "dependency": "redis",
        "available": True,
        "redis_unavailable_ratio": 0.0,
        "status": "healthy",
    }


def test_database_pool_misconfiguration_fails_orders_and_safe_value_recovers(
    client: TestClient,
) -> None:
    misconfigured = client.put(
        "/__admin/configuration/database-pool",
        json={"value": 5},
    )
    failed_order = client.get("/orders/order-001")
    recovered_configuration = client.put(
        "/__admin/configuration/database-pool",
        json={"value": 50},
    )
    recovered_order = client.get("/orders/order-001")

    assert misconfigured.status_code == 200
    assert misconfigured.json() == {
        "key": "database_pool_size",
        "value": 5,
        "database_pool_saturation": 1.0,
        "database_pool_waiters": 5,
        "status": "saturated",
    }
    assert failed_order.status_code == 500
    assert failed_order.json() == {
        "error_code": "database_pool_timeout",
        "message": "数据库连接池容量不足，订单接口模拟获取连接超时",
        "order_id": "order-001",
        "database_pool_size": 5,
    }
    assert recovered_configuration.json()["status"] == "healthy"
    assert recovered_order.status_code == 200


@pytest.mark.parametrize("value", [0, 1001, True, "5"])
def test_database_pool_update_rejects_invalid_values(
    client: TestClient,
    value: object,
) -> None:
    response = client.put(
        "/__admin/configuration/database-pool",
        json={"value": value},
    )

    assert response.status_code == 422


def test_database_pool_compare_and_set_rejects_stale_current_value(
    client: TestClient,
) -> None:
    client.put("/__admin/configuration/database-pool", json={"value": 7})

    response = client.put(
        "/__admin/configuration/database-pool",
        json={"expected_value": 5, "value": 50},
    )
    current = client.get("/__admin/configuration/database-pool")

    assert response.status_code == 409
    assert response.json() == {
        "error_code": "database_pool_value_conflict",
        "message": "database_pool_size 已从预期值 5 变为 7",
        "expected_value": 5,
        "actual_value": 7,
    }
    assert current.json()["value"] == 7


@pytest.mark.parametrize("expected_value", [0, 1001, True, "5"])
def test_database_pool_compare_and_set_rejects_invalid_expected_value(
    client: TestClient,
    expected_value: object,
) -> None:
    response = client.put(
        "/__admin/configuration/database-pool",
        json={"expected_value": expected_value, "value": 50},
    )

    assert response.status_code == 422


def test_redis_unavailable_fails_orders_and_restore_recovers(client: TestClient) -> None:
    unavailable = client.put(
        "/__admin/dependencies/redis",
        json={"available": False},
    )
    failed_order = client.get("/orders/order-001")
    restored = client.put(
        "/__admin/dependencies/redis",
        json={"expected_available": False, "available": True},
    )
    recovered_order = client.get("/orders/order-001")

    assert unavailable.json()["status"] == "unavailable"
    assert unavailable.json()["redis_unavailable_ratio"] == 1.0
    assert failed_order.status_code == 500
    assert failed_order.json() == {
        "error_code": "redis_connection_unavailable",
        "message": "Redis 依赖不可用，订单接口模拟连接失败",
        "order_id": "order-001",
        "dependency": "redis",
    }
    assert restored.json()["status"] == "healthy"
    assert recovered_order.status_code == 200


def test_redis_compare_and_set_rejects_stale_state(client: TestClient) -> None:
    response = client.put(
        "/__admin/dependencies/redis",
        json={"expected_available": False, "available": True},
    )

    assert response.status_code == 409
    assert response.json() == {
        "error_code": "redis_availability_conflict",
        "message": "redis_available 已从预期值 False 变为 True",
        "expected_available": False,
        "actual_available": True,
    }


@pytest.mark.parametrize("value", [0, 1, "true"])
def test_redis_update_rejects_non_boolean_values(client: TestClient, value: object) -> None:
    response = client.put(
        "/__admin/dependencies/redis",
        json={"available": value},
    )

    assert response.status_code == 422


def test_release_fault_has_stable_precedence_over_pool_fault(client: TestClient) -> None:
    client.put("/__admin/configuration/database-pool", json={"value": 5})
    client.put("/__admin/faults/release-500", json={"enabled": True})

    response = client.get("/orders/order-001")

    assert response.status_code == 500
    assert response.json()["error_code"] == "release_500"


def test_pool_fault_has_stable_precedence_over_redis_fault(client: TestClient) -> None:
    client.put("/__admin/configuration/database-pool", json={"value": 5})
    client.put("/__admin/dependencies/redis", json={"available": False})

    response = client.get("/orders/order-001")

    assert response.status_code == 500
    assert response.json()["error_code"] == "database_pool_timeout"


def test_fault_update_rejects_unknown_fields(client: TestClient) -> None:
    response = client.put(
        "/__admin/faults/release-500",
        json={"enabled": True, "failure_rate": 0.5},
    )

    assert response.status_code == 422


def test_order_id_rejects_unsupported_characters(client: TestClient) -> None:
    response = client.get("/orders/order%20with%20space")

    assert response.status_code == 422
