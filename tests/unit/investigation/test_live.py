"""统一真实现场数据源的严格委托测试。"""

from collections.abc import Sequence

import pytest

from opsswarm.investigation.errors import InvestigationDataSourceFailure
from opsswarm.investigation.live import LiveInvestigationDataSource
from opsswarm.investigation.models import EvidenceKind, EvidenceObservation


def _evidence(kind: EvidenceKind, identifier: str) -> tuple[EvidenceObservation, ...]:
    return (
        EvidenceObservation(
            evidence_id=identifier,
            kind=kind,
            source="test-source",
            summary="固定测试证据",
            observed_at="2026-08-31T00:00:00Z",
        ),
    )


class PortSpy:
    """只允许一个指定端口被调用的调查数据源。"""

    def __init__(self, allowed: EvidenceKind) -> None:
        self.allowed = allowed
        self.calls: list[tuple[str, int]] = []

    def query_logs(
        self,
        *,
        service_name: str,
        lookback_minutes: int,
    ) -> Sequence[EvidenceObservation]:
        if self.allowed is not EvidenceKind.LOG:
            raise AssertionError("组合器调用了错误的日志委托")
        self.calls.append((service_name, lookback_minutes))
        return _evidence(EvidenceKind.LOG, "log-live")

    def query_metrics(
        self,
        *,
        service_name: str,
        lookback_minutes: int,
    ) -> Sequence[EvidenceObservation]:
        if self.allowed is not EvidenceKind.METRIC:
            raise AssertionError("组合器调用了错误的指标委托")
        self.calls.append((service_name, lookback_minutes))
        return _evidence(EvidenceKind.METRIC, "metric-live")

    def list_recent_changes(
        self,
        *,
        service_name: str,
        lookback_hours: int,
    ) -> Sequence[EvidenceObservation]:
        if self.allowed is not EvidenceKind.CHANGE:
            raise AssertionError("组合器调用了错误的变更委托")
        self.calls.append((service_name, lookback_hours))
        return _evidence(EvidenceKind.CHANGE, "change-live")


class FailingLogs(PortSpy):
    def query_logs(
        self,
        *,
        service_name: str,
        lookback_minutes: int,
    ) -> Sequence[EvidenceObservation]:
        del service_name, lookback_minutes
        raise InvestigationDataSourceFailure(
            error_code="loki_query_unavailable",
            message="Loki 不可用",
        )


def test_live_source_routes_each_evidence_kind_to_one_fixed_delegate() -> None:
    logs = PortSpy(EvidenceKind.LOG)
    metrics = PortSpy(EvidenceKind.METRIC)
    changes = PortSpy(EvidenceKind.CHANGE)
    source = LiveInvestigationDataSource(logs=logs, metrics=metrics, changes=changes)

    assert source.query_logs(service_name="order-service", lookback_minutes=15)[0].evidence_id == (
        "log-live"
    )
    assert (
        source.query_metrics(service_name="order-service", lookback_minutes=10)[0].evidence_id
        == "metric-live"
    )
    assert (
        source.list_recent_changes(service_name="order-service", lookback_hours=6)[0].evidence_id
        == "change-live"
    )
    assert logs.calls == [("order-service", 15)]
    assert metrics.calls == [("order-service", 10)]
    assert changes.calls == [("order-service", 6)]


def test_live_source_does_not_fallback_when_one_source_fails() -> None:
    source = LiveInvestigationDataSource(
        logs=FailingLogs(EvidenceKind.LOG),
        metrics=PortSpy(EvidenceKind.METRIC),
        changes=PortSpy(EvidenceKind.CHANGE),
    )

    with pytest.raises(InvestigationDataSourceFailure) as captured:
        source.query_logs(service_name="order-service", lookback_minutes=15)

    assert captured.value.error_code == "loki_query_unavailable"
