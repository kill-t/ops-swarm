"""可重复的单 Agent 离线调查场景。"""

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Self
from uuid import UUID

from pydantic import Field, model_validator

from opsswarm.investigation.errors import UnsupportedInvestigationTarget
from opsswarm.investigation.models import (
    EvidenceKind,
    EvidenceObservation,
    InvestigationRequest,
    Recommendation,
    RootCause,
    StrictModel,
)


class InvestigationScenario(StrictModel):
    """冻结输入、证据与期望答案，保证不同 Agent 使用同一把尺子。"""

    scenario_id: str = Field(min_length=1, max_length=100)
    request: InvestigationRequest
    evidence: tuple[EvidenceObservation, ...] = Field(min_length=1)
    expected_root_cause: RootCause
    expected_recommendation: Recommendation
    required_evidence_ids: frozenset[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_expected_evidence(self) -> Self:
        """评测引用只能指向本场景真实存在的冻结证据。"""

        available_ids = {item.evidence_id for item in self.evidence}
        if not self.required_evidence_ids <= available_ids:
            raise ValueError("required_evidence_ids 包含场景中不存在的证据")
        if len(available_ids) != len(self.evidence):
            raise ValueError("场景中的 evidence_id 不能重复")
        return self


class FrozenScenarioDataSource:
    """只读取冻结场景，不依赖网络、数据库或真实模型。"""

    def __init__(self, scenario: InvestigationScenario) -> None:
        self._scenario = scenario

    def query_logs(
        self,
        *,
        service_name: str,
        lookback_minutes: int,
    ) -> Sequence[EvidenceObservation]:
        self._validate_service(service_name)
        del lookback_minutes
        return self._by_kind(EvidenceKind.LOG)

    def query_metrics(
        self,
        *,
        service_name: str,
        lookback_minutes: int,
    ) -> Sequence[EvidenceObservation]:
        self._validate_service(service_name)
        del lookback_minutes
        return self._by_kind(EvidenceKind.METRIC)

    def list_recent_changes(
        self,
        *,
        service_name: str,
        lookback_hours: int,
    ) -> Sequence[EvidenceObservation]:
        self._validate_service(service_name)
        del lookback_hours
        return self._by_kind(EvidenceKind.CHANGE)

    def _validate_service(self, service_name: str) -> None:
        if service_name != self._scenario.request.service_name:
            raise UnsupportedInvestigationTarget(service_name)

    def _by_kind(self, kind: EvidenceKind) -> tuple[EvidenceObservation, ...]:
        return tuple(item for item in self._scenario.evidence if item.kind is kind)


RELEASE_500_SCENARIO = InvestigationScenario(
    scenario_id="release-500-baseline-v1",
    request=InvestigationRequest(
        incident_id=UUID("00000000-0000-0000-0000-000000000001"),
        service_name="order-service",
        title="订单接口在新版本发布后大量返回 500",
        started_at=datetime(2026, 1, 1, 8, 5, tzinfo=UTC),
    ),
    evidence=(
        EvidenceObservation(
            evidence_id="log-order-runtime-error",
            kind=EvidenceKind.LOG,
            source="frozen-log-store",
            summary="order-service 的 /orders 请求持续出现同一处未处理运行时异常。",
            observed_at=datetime(2026, 1, 1, 8, 6, tzinfo=UTC),
            attributes={
                "route": "/orders/{id}",
                "status_code": 500,
                "release_version": "2026.01.01-rc1",
                "error_type": "RuntimeError",
            },
        ),
        EvidenceObservation(
            evidence_id="metric-order-error-spike",
            kind=EvidenceKind.METRIC,
            source="frozen-metric-store",
            summary="order-service 的 5xx 比例从 0.2% 跃升至 92%，数据库与 Redis 健康指标正常。",
            observed_at=datetime(2026, 1, 1, 8, 6, tzinfo=UTC),
            attributes={
                "http_5xx_ratio_before": 0.002,
                "http_5xx_ratio_after": 0.92,
                "postgres_healthy": True,
                "redis_healthy": True,
            },
        ),
        EvidenceObservation(
            evidence_id="change-order-release-rc1",
            kind=EvidenceKind.CHANGE,
            source="frozen-change-store",
            summary="order-service 在错误率上升前一分钟从 stable 发布为 2026.01.01-rc1。",
            observed_at=datetime(2026, 1, 1, 8, 4, tzinfo=UTC),
            attributes={
                "change_type": "deployment",
                "from_version": "stable",
                "to_version": "2026.01.01-rc1",
                "completed": True,
            },
        ),
    ),
    expected_root_cause=RootCause.RELEASE_500_REGRESSION,
    expected_recommendation=Recommendation.ROLLBACK_RELEASE,
    required_evidence_ids=frozenset(
        {
            "log-order-runtime-error",
            "metric-order-error-spike",
            "change-order-release-rc1",
        }
    ),
)


DATABASE_POOL_SCENARIO = InvestigationScenario(
    scenario_id="database-pool-misconfiguration-v1",
    request=InvestigationRequest(
        incident_id=UUID("00000000-0000-0000-0000-000000000002"),
        service_name="order-service",
        title="订单接口在连接池配置变更后大量超时",
        started_at=datetime(2026, 1, 2, 9, 5, tzinfo=UTC),
    ),
    evidence=(
        EvidenceObservation(
            evidence_id="log-order-database-pool-timeout",
            kind=EvidenceKind.LOG,
            source="frozen-log-store",
            summary="order-service 持续出现 database connection pool acquire timeout。",
            observed_at=datetime(2026, 1, 2, 9, 6, tzinfo=UTC),
            attributes={
                "route": "/orders/{id}",
                "status_code": 500,
                "error_type": "database_pool_timeout",
                "timeout_seconds": 2,
            },
        ),
        EvidenceObservation(
            evidence_id="metric-order-database-pool-saturation",
            kind=EvidenceKind.METRIC,
            source="frozen-metric-store",
            summary="连接池持续满载且等待请求增加，但 PostgreSQL 健康检查正常。",
            observed_at=datetime(2026, 1, 2, 9, 6, tzinfo=UTC),
            attributes={
                "database_pool_saturation": 1.0,
                "database_pool_waiters": 47,
                "postgres_healthy": True,
                "redis_healthy": True,
            },
        ),
        EvidenceObservation(
            evidence_id="change-order-database-pool-size",
            kind=EvidenceKind.CHANGE,
            source="frozen-change-store",
            summary="事故前一分钟把 order-service 的数据库连接池上限从 50 调整为 5。",
            observed_at=datetime(2026, 1, 2, 9, 4, tzinfo=UTC),
            attributes={
                "change_type": "configuration",
                "key": "database_pool_size",
                "from_value": 50,
                "to_value": 5,
                "completed": True,
            },
        ),
    ),
    expected_root_cause=RootCause.DATABASE_POOL_MISCONFIGURATION,
    expected_recommendation=Recommendation.CONTINUE_INVESTIGATION,
    required_evidence_ids=frozenset(
        {
            "log-order-database-pool-timeout",
            "metric-order-database-pool-saturation",
        }
    ),
)


REDIS_UNAVAILABLE_SCENARIO = InvestigationScenario(
    scenario_id="redis-unavailable-v1",
    request=InvestigationRequest(
        incident_id=UUID("00000000-0000-0000-0000-000000000003"),
        service_name="order-service",
        title="Redis 不可用导致订单接口失败",
        started_at=datetime(2026, 1, 3, 10, 5, tzinfo=UTC),
    ),
    evidence=(
        EvidenceObservation(
            evidence_id="log-order-redis-connection-failed",
            kind=EvidenceKind.LOG,
            source="frozen-log-store",
            summary="order-service 访问 Redis 时持续出现 connection unavailable。",
            observed_at=datetime(2026, 1, 3, 10, 6, tzinfo=UTC),
            attributes={
                "route": "/orders/{id}",
                "status_code": 500,
                "error_type": "redis_connection_unavailable",
                "dependency": "redis",
            },
        ),
        EvidenceObservation(
            evidence_id="metric-order-redis-health-failed",
            kind=EvidenceKind.METRIC,
            source="frozen-metric-store",
            summary="Redis 健康探针失败，PostgreSQL 和订单服务进程仍然存活。",
            observed_at=datetime(2026, 1, 3, 10, 6, tzinfo=UTC),
            attributes={
                "redis_healthy": False,
                "postgres_healthy": True,
                "order_process_healthy": True,
                "http_5xx_ratio": 0.88,
            },
        ),
        EvidenceObservation(
            evidence_id="change-redis-maintenance-restart",
            kind=EvidenceKind.CHANGE,
            source="frozen-change-store",
            summary="Redis 节点在事故前两分钟进入维护重启，order-service 没有新版本发布。",
            observed_at=datetime(2026, 1, 3, 10, 3, tzinfo=UTC),
            attributes={
                "change_type": "dependency_maintenance",
                "dependency": "redis",
                "operation": "restart",
                "order_release_changed": False,
            },
        ),
    ),
    expected_root_cause=RootCause.REDIS_UNAVAILABLE,
    expected_recommendation=Recommendation.CONTINUE_INVESTIGATION,
    required_evidence_ids=frozenset(
        {
            "log-order-redis-connection-failed",
            "metric-order-redis-health-failed",
        }
    ),
)


# v1 场景保持不可变；v2 只升级建议标签和场景身份，证据与根因真值不变。
RELEASE_500_SCENARIO_V2 = RELEASE_500_SCENARIO.model_copy(
    update={"scenario_id": "release-500-baseline-v2"}
)
DATABASE_POOL_SCENARIO_V2 = DATABASE_POOL_SCENARIO.model_copy(
    update={
        "scenario_id": "database-pool-misconfiguration-v2",
        "expected_recommendation": Recommendation.ROLLBACK_CONFIGURATION,
    }
)
REDIS_UNAVAILABLE_SCENARIO_V2 = REDIS_UNAVAILABLE_SCENARIO.model_copy(
    update={
        "scenario_id": "redis-unavailable-v2",
        "expected_recommendation": Recommendation.RESTORE_DEPENDENCY,
    }
)
