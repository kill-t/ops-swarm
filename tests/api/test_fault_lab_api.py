"""故障演练服务 API 测试。"""

from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from opsswarm.fault_lab.app import create_app
from opsswarm.fault_lab.controller import FaultController
from opsswarm.fault_lab.runtime_metadata import RuntimeMetadataSettings

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

    assert response.status_code == 503
    assert response.json() == {
        "error_code": "runtime_metadata_unavailable",
        "message": "运行元数据未启用",
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
