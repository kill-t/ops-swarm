"""故障演练服务 API 测试。"""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from opsswarm.fault_lab.app import create_app


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(create_app()) as test_client:
        yield test_client


def test_health_endpoint(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"service": "order-service", "status": "healthy"}


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


def test_release_fault_has_stable_precedence_over_pool_fault(client: TestClient) -> None:
    client.put("/__admin/configuration/database-pool", json={"value": 5})
    client.put("/__admin/faults/release-500", json={"enabled": True})

    response = client.get("/orders/order-001")

    assert response.status_code == 500
    assert response.json()["error_code"] == "release_500"


def test_fault_update_rejects_unknown_fields(client: TestClient) -> None:
    response = client.put(
        "/__admin/faults/release-500",
        json={"enabled": True, "failure_rate": 0.5},
    )

    assert response.status_code == 422


def test_order_id_rejects_unsupported_characters(client: TestClient) -> None:
    response = client.get("/orders/order%20with%20space")

    assert response.status_code == 422
