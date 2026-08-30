"""变更服务只读调查数据源与 LangChain 工具测试。"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx2
import pytest
from pydantic import ValidationError

from opsswarm.change_records.models import (
    ChangeRecord,
    ChangeRecordListResponse,
    ConfigurationChange,
    DependencyMaintenanceChange,
    DeploymentChange,
)
from opsswarm.investigation.changes import (
    MAX_RESPONSE_BYTES,
    ChangeApiOnlyInvestigationDataSource,
    ChangeInvestigationSettings,
)
from opsswarm.investigation.errors import (
    InvestigationDataSourceFailure,
    UnsupportedInvestigationTarget,
)
from opsswarm.investigation.models import EvidenceKind
from opsswarm.investigation.tools import EvidenceCollector, create_investigation_tools

NOW = 1_788_084_800
GENERATED_AT = datetime.fromtimestamp(NOW, UTC)


def _record(
    identifier: int,
    change: DeploymentChange | ConfigurationChange | DependencyMaintenanceChange,
    *,
    minutes_ago: int,
) -> ChangeRecord:
    occurred_at = GENERATED_AT - timedelta(minutes=minutes_ago)
    return ChangeRecord(
        record_id=UUID(int=identifier),
        occurred_at=occurred_at,
        recorded_at=occurred_at,
        change=change,
    )


def _response(
    *,
    items: tuple[ChangeRecord, ...] | None = None,
    truncated: bool = False,
    generated_at: datetime = GENERATED_AT,
) -> ChangeRecordListResponse:
    default_items = (
        _record(
            3,
            DependencyMaintenanceChange(external_change_id="redis-1"),
            minutes_ago=1,
        ),
        _record(
            2,
            ConfigurationChange(
                external_change_id="pool-1",
                from_value=50,
                to_value=5,
            ),
            minutes_ago=2,
        ),
        _record(
            1,
            DeploymentChange(
                external_change_id="release-1",
                from_version="stable",
                to_version="rc1",
            ),
            minutes_ago=3,
        ),
    )
    return ChangeRecordListResponse(
        generated_at=generated_at,
        items=default_items if items is None else items,
        truncated=truncated,
    )


def _transport(
    response: ChangeRecordListResponse | None = None,
    *,
    status_code: int = 200,
) -> tuple[httpx2.MockTransport, list[httpx2.Request]]:
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        if status_code != 200:
            return httpx2.Response(status_code, json={"status": "error"})
        return httpx2.Response(
            200,
            content=(response or _response()).model_dump_json(),
            headers={"content-type": "application/json"},
        )

    return httpx2.MockTransport(handler), requests


def _source(transport: httpx2.BaseTransport) -> ChangeApiOnlyInvestigationDataSource:
    return ChangeApiOnlyInvestigationDataSource(
        ChangeInvestigationSettings(change_base_url="http://change-service:8000"),
        transport=transport,
        clock=lambda: float(NOW),
    )


def test_change_tool_runs_fixed_query_and_collects_three_normalized_evidence() -> None:
    transport, requests = _transport()
    collector = EvidenceCollector()
    tools = create_investigation_tools(_source(transport), collector)
    changes_tool = next(tool for tool in tools if tool.name == "list_recent_changes")

    result = changes_tool.invoke({"service_name": "order-service", "lookback_hours": 6})

    assert len(result) == 3
    assert len(collector.evidence_items) == 3
    assert {item.kind for item in collector.evidence_items} == {EvidenceKind.CHANGE}
    assert {item.source for item in collector.evidence_items} == {"change-simulator"}
    attributes_by_type = {
        item.attributes["change_type"]: item.attributes for item in collector.evidence_items
    }
    assert attributes_by_type["deployment"]["to_version"] == "rc1"
    assert attributes_by_type["configuration"]["to_value"] == 5
    assert attributes_by_type["dependency_maintenance"]["dependency"] == "redis"
    assert len(requests) == 1
    assert requests[0].url.path == "/api/v1/changes"
    assert dict(requests[0].url.params) == {
        "service_name": "order-service",
        "lookback_hours": "6",
    }


def test_empty_response_is_honest_empty_evidence() -> None:
    transport, _ = _transport(_response(items=()))

    result = _source(transport).list_recent_changes(
        service_name="order-service",
        lookback_hours=6,
    )

    assert result == ()


def test_logs_and_metrics_return_empty_without_network_call() -> None:
    transport, requests = _transport()
    source = _source(transport)

    assert source.query_logs(service_name="order-service", lookback_minutes=15) == ()
    assert source.query_metrics(service_name="order-service", lookback_minutes=15) == ()
    assert requests == []


def test_unsupported_service_is_rejected_before_network_call() -> None:
    transport, requests = _transport()

    with pytest.raises(UnsupportedInvestigationTarget):
        _source(transport).list_recent_changes(
            service_name="payment-service",
            lookback_hours=6,
        )

    assert requests == []


@pytest.mark.parametrize("lookback_hours", [0, 25, True, 1.5])
def test_direct_change_query_revalidates_lookback(lookback_hours: object) -> None:
    transport, requests = _transport()

    with pytest.raises(ValueError):
        _source(transport).list_recent_changes(
            service_name="order-service",
            lookback_hours=lookback_hours,  # type: ignore[arg-type]
        )

    assert requests == []


@pytest.mark.parametrize(
    ("response", "error_code"),
    [
        (_response(truncated=True), "change_result_truncated"),
        (
            _response(generated_at=GENERATED_AT - timedelta(seconds=11)),
            "change_stale_response",
        ),
        (
            _response(
                items=(
                    _record(
                        1,
                        DeploymentChange(
                            external_change_id="release-old",
                            from_version="stable",
                            to_version="rc1",
                        ),
                        minutes_ago=7 * 60,
                    ),
                )
            ),
            "change_invalid_timestamp",
        ),
        (
            _response(
                items=(
                    _record(
                        1,
                        DeploymentChange(
                            external_change_id="release-1",
                            from_version="stable",
                            to_version="rc1",
                        ),
                        minutes_ago=1,
                    ),
                    _record(
                        1,
                        DeploymentChange(
                            external_change_id="release-2",
                            from_version="stable",
                            to_version="rc2",
                        ),
                        minutes_ago=2,
                    ),
                )
            ),
            "change_duplicate_record",
        ),
    ],
)
def test_incomplete_or_untrusted_response_fails_closed(
    response: ChangeRecordListResponse,
    error_code: str,
) -> None:
    transport, _ = _transport(response)

    with pytest.raises(InvestigationDataSourceFailure) as captured:
        _source(transport).list_recent_changes(
            service_name="order-service",
            lookback_hours=6,
        )

    assert captured.value.error_code == error_code


def test_invalid_json_and_large_response_fail_closed() -> None:
    invalid_transport = httpx2.MockTransport(
        lambda request: httpx2.Response(200, content=b"not-json" + request.url.path.encode())
    )
    large_transport = httpx2.MockTransport(
        lambda request: httpx2.Response(200, content=b"x" * (MAX_RESPONSE_BYTES + 1))
    )

    with pytest.raises(InvestigationDataSourceFailure) as invalid:
        _source(invalid_transport).list_recent_changes(
            service_name="order-service",
            lookback_hours=6,
        )
    with pytest.raises(InvestigationDataSourceFailure) as large:
        _source(large_transport).list_recent_changes(
            service_name="order-service",
            lookback_hours=6,
        )

    assert invalid.value.error_code == "change_invalid_response"
    assert large.value.error_code == "change_response_too_large"


def test_http_failure_is_not_misreported_as_empty_evidence() -> None:
    transport, _ = _transport(status_code=503)

    with pytest.raises(InvestigationDataSourceFailure) as captured:
        _source(transport).list_recent_changes(
            service_name="order-service",
            lookback_hours=6,
        )

    assert captured.value.error_code == "change_unexpected_status"
    assert captured.value.http_status == 503


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:18002",
        "http://example.com:8000",
        "http://127.0.0.1:18002/api/v1/changes",
        "http://user:secret@127.0.0.1:18002",
        "http://127.0.0.1:8000",
        "http://change-service:18002",
    ],
)
def test_settings_reject_unsafe_change_url(url: str) -> None:
    with pytest.raises(ValidationError):
        ChangeInvestigationSettings(change_base_url=url)
