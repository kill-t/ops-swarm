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


def test_fault_update_rejects_unknown_fields(client: TestClient) -> None:
    response = client.put(
        "/__admin/faults/release-500",
        json={"enabled": True, "failure_rate": 0.5},
    )

    assert response.status_code == 422


def test_order_id_rejects_unsupported_characters(client: TestClient) -> None:
    response = client.get("/orders/order%20with%20space")

    assert response.status_code == 422
