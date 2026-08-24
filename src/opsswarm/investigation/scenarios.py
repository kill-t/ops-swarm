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
