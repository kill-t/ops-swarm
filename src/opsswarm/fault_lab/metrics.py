"""故障实验室暴露给 Prometheus 的低基数指标。"""

from typing import Literal

from prometheus_client import CollectorRegistry, Counter, Gauge, generate_latest

from opsswarm.fault_lab.controller import FaultSnapshot

OrderResult = Literal["success", "failed"]
OrderFailureReason = Literal["none", "release_500", "database_pool_timeout", "redis_unavailable"]


class FaultLabMetrics:
    """每个应用实例持有独立注册表，避免测试与多实例互相污染。"""

    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        self._http_5xx_ratio = Gauge(
            "opsswarm_fault_lab_http_5xx_ratio",
            "当前确定性订单请求是否会返回 5xx，取值为 0 或 1。",
            ("service",),
            registry=self.registry,
        )
        self._database_pool_saturation = Gauge(
            "opsswarm_fault_lab_database_pool_saturation",
            "固定演练负载下的数据库连接池饱和度。",
            ("service",),
            registry=self.registry,
        )
        self._redis_unavailable_ratio = Gauge(
            "opsswarm_fault_lab_redis_unavailable_ratio",
            "真实 Redis PING 不可用状态，取值为 0 或 1。",
            ("service",),
            registry=self.registry,
        )
        self._order_requests = Counter(
            "opsswarm_fault_lab_order_requests_total",
            "故障实验室订单请求总数。",
            ("service", "result", "failure_reason"),
            registry=self.registry,
        )

    def render(self, *, snapshot: FaultSnapshot, redis_available: bool) -> bytes:
        """用当前领域状态和真实 Redis 探测刷新快照指标。"""

        order_fails = (
            snapshot.release_500_enabled
            or snapshot.database_pool_timeout_enabled
            or not redis_available
        )
        self._http_5xx_ratio.labels(service="order-service").set(1.0 if order_fails else 0.0)
        self._database_pool_saturation.labels(service="order-service").set(
            snapshot.database_pool_saturation
        )
        self._redis_unavailable_ratio.labels(service="order-service").set(
            0.0 if redis_available else 1.0
        )
        return generate_latest(self.registry)

    def record_order(self, *, result: OrderResult, failure_reason: OrderFailureReason) -> None:
        """只允许固定标签值，避免订单 ID 等高基数数据进入指标。"""

        self._order_requests.labels(
            service="order-service",
            result=result,
            failure_reason=failure_reason,
        ).inc()
