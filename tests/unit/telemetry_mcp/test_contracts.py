from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from opsswarm.telemetry_mcp.contracts import QueryServiceLogsRequest

START = datetime(2026, 10, 4, 1, 0, tzinfo=UTC)


def test_request_accepts_fixed_service_and_utc_window() -> None:
    request = QueryServiceLogsRequest(
        service_name="order-service",
        window_start=START,
        window_end=START + timedelta(minutes=15),
    )

    assert request.window_start.tzinfo is UTC
    assert request.window_end - request.window_start == timedelta(minutes=15)


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"service_name": "billing-service"}, "order-service"),
        ({"window_end": START}, "必须为正"),
        ({"window_end": START + timedelta(minutes=61)}, "不超过 60 分钟"),
        ({"window_end": START + timedelta(seconds=90)}, "完整分钟"),
        (
            {
                "window_start": START.astimezone(timezone(timedelta(hours=8))),
                "window_end": (START + timedelta(minutes=15)).astimezone(
                    timezone(timedelta(hours=8))
                ),
            },
            "UTC 时区",
        ),
    ],
)
def test_request_rejects_scope_expansion(updates: dict[str, object], message: str) -> None:
    values: dict[str, object] = {
        "service_name": "order-service",
        "window_start": START,
        "window_end": START + timedelta(minutes=15),
    }
    values.update(updates)

    with pytest.raises(ValidationError, match=message):
        QueryServiceLogsRequest.model_validate(values)


def test_request_rejects_arbitrary_query_controls() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        QueryServiceLogsRequest.model_validate(
            {
                "service_name": "order-service",
                "window_start": START,
                "window_end": START + timedelta(minutes=15),
                "url": "http://attacker.invalid",
                "logql": '{service=~".+"}',
                "limit": 10_000,
            }
        )
