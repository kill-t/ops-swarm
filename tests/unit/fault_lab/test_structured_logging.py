"""故障实验室结构化事故日志契约测试。"""

import json
from datetime import UTC, datetime
from io import StringIO

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from opsswarm.fault_lab.app import create_app
from opsswarm.fault_lab.controller import FaultController
from opsswarm.fault_lab.structured_logging import (
    FaultKind,
    FaultLabEventLogger,
    FaultLabLoggingSettings,
)

OBSERVED_AT = datetime(2026, 8, 30, 10, 30, tzinfo=UTC)


def test_three_faults_emit_finite_json_contract_without_order_id() -> None:
    stream = StringIO()
    event_logger = FaultLabEventLogger(stream=stream, clock=lambda: OBSERVED_AT)
    with TestClient(create_app(FaultController(), event_logger=event_logger)) as client:
        client.put("/__admin/faults/release-500", json={"enabled": True})
        client.get("/orders/private-order-001")
        client.put("/__admin/faults/release-500", json={"enabled": False})
        client.put("/__admin/configuration/database-pool", json={"value": 5})
        client.get("/orders/private-order-002")
        client.put("/__admin/configuration/database-pool", json={"value": 50})
        client.put("/__admin/dependencies/redis", json={"available": False})
        client.get("/orders/private-order-003")

    events = [json.loads(line) for line in stream.getvalue().splitlines()]

    assert events == [
        {
            "error_type": "RuntimeError",
            "event": "order_request_failed",
            "fault": "release_500",
            "level": "warning",
            "release_version": "2026.01.01-rc1",
            "route": "/orders/{id}",
            "service": "order-service",
            "status_code": 500,
            "timestamp": "2026-08-30T10:30:00.000Z",
        },
        {
            "database_pool_size": 5,
            "database_pool_waiters": 5,
            "error_type": "database_pool_timeout",
            "event": "order_request_failed",
            "fault": "database_pool_timeout",
            "level": "warning",
            "route": "/orders/{id}",
            "service": "order-service",
            "status_code": 500,
            "timestamp": "2026-08-30T10:30:00.000Z",
        },
        {
            "dependency": "redis",
            "error_type": "redis_connection_unavailable",
            "event": "order_request_failed",
            "fault": "redis_unavailable",
            "level": "warning",
            "route": "/orders/{id}",
            "service": "order-service",
            "status_code": 500,
            "timestamp": "2026-08-30T10:30:00.000Z",
        },
    ]
    assert "private-order" not in stream.getvalue()


def test_naive_clock_is_rejected_before_log_write() -> None:
    stream = StringIO()
    event_logger = FaultLabEventLogger(
        stream=stream,
        clock=lambda: datetime(2026, 8, 30, 10, 30),
    )

    with pytest.raises(ValueError, match="必须返回带时区时间"):
        event_logger.order_failure(
            fault=FaultKind.RELEASE_500,
            snapshot=FaultController().snapshot(),
        )

    assert stream.getvalue() == ""


@pytest.mark.parametrize(
    "path",
    [
        "order-service.jsonl",
        "/tmp/order-service.jsonl",
        "/var/log/opsswarm/other-service.jsonl",
    ],
)
def test_settings_reject_arbitrary_log_path(path: str) -> None:
    with pytest.raises(ValidationError):
        FaultLabLoggingSettings(log_path=path)
