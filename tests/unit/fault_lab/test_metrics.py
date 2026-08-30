"""故障实验室 Prometheus 指标测试。"""

from prometheus_client.parser import text_string_to_metric_families

from opsswarm.fault_lab.controller import FaultController
from opsswarm.fault_lab.metrics import FaultLabMetrics


def _sample_value(payload: bytes, name: str, labels: dict[str, str]) -> float:
    for family in text_string_to_metric_families(payload.decode("utf-8")):
        for sample in family.samples:
            if sample.name == name and sample.labels == labels:
                return float(sample.value)
    raise AssertionError(f"未找到指标样本：{name} {labels}")


def test_metrics_render_current_fault_state() -> None:
    metrics = FaultLabMetrics()
    snapshot = FaultController(database_pool_size=5).snapshot()

    payload = metrics.render(snapshot=snapshot, redis_available=False)

    assert (
        _sample_value(
            payload,
            "opsswarm_fault_lab_http_5xx_ratio",
            {"service": "order-service"},
        )
        == 1.0
    )
    assert (
        _sample_value(
            payload,
            "opsswarm_fault_lab_database_pool_saturation",
            {"service": "order-service"},
        )
        == 1.0
    )
    assert (
        _sample_value(
            payload,
            "opsswarm_fault_lab_redis_unavailable_ratio",
            {"service": "order-service"},
        )
        == 1.0
    )


def test_order_counter_uses_only_fixed_low_cardinality_labels() -> None:
    metrics = FaultLabMetrics()
    metrics.record_order(result="success", failure_reason="none")
    metrics.record_order(result="failed", failure_reason="redis_unavailable")

    payload = metrics.render(
        snapshot=FaultController().snapshot(),
        redis_available=True,
    )

    assert (
        _sample_value(
            payload,
            "opsswarm_fault_lab_order_requests_total",
            {
                "service": "order-service",
                "result": "success",
                "failure_reason": "none",
            },
        )
        == 1.0
    )
    assert (
        _sample_value(
            payload,
            "opsswarm_fault_lab_order_requests_total",
            {
                "service": "order-service",
                "result": "failed",
                "failure_reason": "redis_unavailable",
            },
        )
        == 1.0
    )
