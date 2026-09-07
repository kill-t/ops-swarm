"""历史变更不混入当前清单、迟到入库或其他查询窗口。"""

from datetime import timedelta

import httpx2
import pytest

from opsswarm.investigation.changes import ChangeApiOnlyInvestigationDataSource
from opsswarm.investigation.errors import (
    InvestigationDataSourceFailure,
    UnsupportedInvestigationTarget,
)
from opsswarm.observation_window import ObservationWindow
from tests.unit.investigation.test_changes import GENERATED_AT, NOW, _response

WINDOW = ObservationWindow(
    window_start=GENERATED_AT - timedelta(hours=6),
    window_end=GENERATED_AT,
)


def payload():
    return {
        **_response(generated_at=GENERATED_AT + timedelta(hours=1)).model_dump(mode="json"),
        "window": WINDOW.model_dump(mode="json"),
    }


def source_for(body, *, status=200):
    requests = []

    def read(request):
        requests.append(request)
        return httpx2.Response(status, json=body)

    def no_runtime(request):
        raise AssertionError("历史事件查询不允许请求当前运行清单")

    return ChangeApiOnlyInvestigationDataSource(
        transport=httpx2.MockTransport(read),
        runtime_transport=httpx2.MockTransport(no_runtime),
        clock=lambda: NOW + 3600,
    ), requests


def test_historical_changes_echo_window_and_never_claim_current_deployment():
    source, requests = source_for(payload())
    observations = source.list_changes_in_window(service_name="order-service", window=WINDOW)
    assert len(observations) == 3
    deployment = next(e for e in observations if e.attributes["change_type"] == "deployment")
    assert deployment.attributes["deployment_position"] == "historical"
    assert deployment.attributes["runtime_artifact_status"] == "historical"
    assert (
        deployment.attributes["observation_semantics"] == "historical_change_recorded_by_window_end"
    )
    assert "runtime_inventory_reported_at" not in deployment.attributes
    assert "不与当前运行实例比较" in deployment.summary
    assert all(e.observed_at <= GENERATED_AT for e in observations)
    assert all(
        e.attributes["response_generated_at"] != e.attributes["window_end"] for e in observations
    )
    assert requests[0].url.path == "/api/v1/changes/window"
    assert dict(requests[0].url.params) == {
        "service_name": "order-service",
        **WINDOW.model_dump(mode="json"),
    }


@pytest.mark.parametrize(
    "invalid,code",
    [
        ("window", "change_window_mismatch"),
        ("missing_window", "change_invalid_response"),
        ("generation_before_end", "change_invalid_response"),
        ("late_record", "change_invalid_timestamp"),
        ("outside_event", "change_invalid_timestamp"),
        ("stale", "change_stale_response"),
        ("duplicate", "change_duplicate_record"),
        ("truncated", "change_result_truncated"),
        ("unavailable", "change_unexpected_status"),
    ],
)
def test_invalid_window_response_fails_without_fallback(invalid, code):
    body = payload()
    if invalid == "window":
        body["window"]["window_end"] = (GENERATED_AT - timedelta(seconds=1)).isoformat()
    elif invalid == "missing_window":
        body.pop("window")
    elif invalid in ("late_record", "outside_event"):
        body["items"][0]["recorded_at"] = (GENERATED_AT + timedelta(seconds=1)).isoformat()
        if invalid == "outside_event":
            body["items"][0]["occurred_at"] = body["items"][0]["recorded_at"]
    elif invalid == "stale":
        body["generated_at"] = GENERATED_AT.isoformat()
    elif invalid == "generation_before_end":
        body["generated_at"] = (GENERATED_AT - timedelta(seconds=1)).isoformat()
    elif invalid == "duplicate":
        body["items"].append(body["items"][0])
    elif invalid == "truncated":
        body["truncated"] = True
    source, requests = source_for(body, status=503 if invalid == "unavailable" else 200)
    with pytest.raises(InvestigationDataSourceFailure) as caught:
        source.list_changes_in_window(service_name="order-service", window=WINDOW)
    assert caught.value.error_code == code
    assert len(requests) == 1 and requests[0].url.path.endswith("/window")


def test_empty_window_is_distinct_from_source_failure_and_future_is_rejected():
    body = payload()
    body["items"] = []
    source, requests = source_for(body)
    assert source.list_changes_in_window(service_name="order-service", window=WINDOW) == ()
    future = ObservationWindow(
        window_start=GENERATED_AT,
        window_end=GENERATED_AT + timedelta(hours=2),
    )
    with pytest.raises(ValueError, match="截止"):
        source.list_changes_in_window(service_name="order-service", window=future)
    with pytest.raises(UnsupportedInvestigationTarget):
        source.list_changes_in_window(service_name="other-service", window=WINDOW)
    assert len(requests) == 1
