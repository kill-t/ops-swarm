"""根因假设质疑规则的纯单元测试。"""

import pytest
from pydantic import ValidationError

from opsswarm.investigation.challenge import (
    CHALLENGE_RULE_VERSION,
    challenge_investigation,
    validate_hypothesis_challenge,
)
from opsswarm.investigation.models import (
    EvidenceKind,
    EvidenceObservation,
    EvidenceRelation,
    HypothesisStatus,
    InvestigationArtifacts,
    InvestigationConclusion,
    InvestigationMetrics,
    InvestigationRunResult,
    RootCause,
)
from opsswarm.investigation.scenarios import RELEASE_500_SCENARIO
from opsswarm.worker import ProcessorOutcome


def build_artifacts(
    evidence_items: tuple[EvidenceObservation, ...],
    root_cause: RootCause = RootCause.RELEASE_500_REGRESSION,
) -> InvestigationArtifacts:
    evidence_ids = tuple(sorted(item.evidence_id for item in evidence_items))
    return InvestigationArtifacts(
        result=InvestigationRunResult(
            conclusion=InvestigationConclusion(
                root_cause=root_cause,
                confidence=0.96,
                summary="新发布版本导致订单接口大量返回 500。",
                evidence_ids=evidence_ids,
                recommendation="rollback_release",
                requires_human_approval=True,
            ),
            returned_evidence_ids=evidence_ids,
            tool_call_count=len(evidence_items),
        ),
        evidence_items=evidence_items,
    )


def test_release_hypothesis_requires_two_independent_support_kinds() -> None:
    challenge = challenge_investigation(build_artifacts(RELEASE_500_SCENARIO.evidence))

    assert challenge.status is HypothesisStatus.SUPPORTED
    assert challenge.rule_version == CHALLENGE_RULE_VERSION
    assert {item.relation for item in challenge.evidence_assessments} == {EvidenceRelation.SUPPORTS}
    assert {item.evidence_id for item in challenge.evidence_assessments} == (
        RELEASE_500_SCENARIO.required_evidence_ids
    )


def test_unhealthy_dependency_contests_release_hypothesis() -> None:
    metric = next(
        item for item in RELEASE_500_SCENARIO.evidence if item.kind is EvidenceKind.METRIC
    )
    contested_metric = EvidenceObservation.model_validate(
        {
            **metric.model_dump(mode="json"),
            "attributes": {**metric.attributes, "redis_healthy": False},
        }
    )
    evidence = tuple(
        contested_metric if item.kind is EvidenceKind.METRIC else item
        for item in RELEASE_500_SCENARIO.evidence
    )

    challenge = challenge_investigation(build_artifacts(evidence))

    assert challenge.status is HypothesisStatus.CONTESTED
    metric_assessment = next(
        item
        for item in challenge.evidence_assessments
        if item.evidence_id == contested_metric.evidence_id
    )
    assert metric_assessment.relation is EvidenceRelation.REFUTES
    assert metric_assessment.rule_code == "release_dependency_unhealthy"


def test_single_support_kind_is_insufficient() -> None:
    log_evidence = tuple(
        item for item in RELEASE_500_SCENARIO.evidence if item.kind is EvidenceKind.LOG
    )

    challenge = challenge_investigation(build_artifacts(log_evidence))

    assert challenge.status is HypothesisStatus.INSUFFICIENT_EVIDENCE


def test_pool_saturation_supports_database_hypothesis_even_when_database_is_healthy() -> None:
    metric = EvidenceObservation(
        evidence_id="metric-database-pool",
        kind=EvidenceKind.METRIC,
        source="fixture-metrics",
        summary="连接池持续饱和。",
        observed_at=RELEASE_500_SCENARIO.evidence[0].observed_at,
        attributes={"postgres_healthy": True, "database_pool_saturation": 0.97},
    )
    log = EvidenceObservation(
        evidence_id="log-database-timeout",
        kind=EvidenceKind.LOG,
        source="fixture-logs",
        summary="获取数据库连接超时。",
        observed_at=RELEASE_500_SCENARIO.evidence[0].observed_at,
        attributes={"error_type": "pool_timeout"},
    )

    challenge = challenge_investigation(
        build_artifacts(
            (metric, log),
            root_cause=RootCause.DATABASE_POOL_MISCONFIGURATION,
        )
    )

    assert challenge.status is HypothesisStatus.SUPPORTED
    assert {item.rule_code for item in challenge.evidence_assessments} == {
        "database_pool_saturated",
        "database_connection_error",
    }


def test_metric_and_log_support_redis_unavailable_hypothesis() -> None:
    metric = EvidenceObservation(
        evidence_id="metric-redis-health",
        kind=EvidenceKind.METRIC,
        source="fixture-metrics",
        summary="Redis 健康检查失败。",
        observed_at=RELEASE_500_SCENARIO.evidence[0].observed_at,
        attributes={"redis_healthy": False},
    )
    log = EvidenceObservation(
        evidence_id="log-redis-timeout",
        kind=EvidenceKind.LOG,
        source="fixture-logs",
        summary="Redis connection timeout。",
        observed_at=RELEASE_500_SCENARIO.evidence[0].observed_at,
        attributes={"error_type": "redis_unavailable"},
    )

    challenge = challenge_investigation(
        build_artifacts((metric, log), root_cause=RootCause.REDIS_UNAVAILABLE)
    )

    assert challenge.status is HypothesisStatus.SUPPORTED
    assert {item.relation for item in challenge.evidence_assessments} == {EvidenceRelation.SUPPORTS}


def test_persistence_validation_rejects_tampered_challenge() -> None:
    artifacts = build_artifacts(RELEASE_500_SCENARIO.evidence)
    challenge = challenge_investigation(artifacts)
    tampered = challenge.model_copy(update={"status": HypothesisStatus.CONTESTED})

    with pytest.raises(ValueError, match="与当前确定性规则不一致"):
        validate_hypothesis_challenge(artifacts, tampered)

    with pytest.raises(ValidationError, match="与当前确定性规则不一致"):
        ProcessorOutcome(
            result=artifacts.result,
            evidence_items=artifacts.evidence_items,
            hypothesis_challenge=tampered,
            metrics=InvestigationMetrics(
                top1_accuracy=1,
                evidence_recall=1,
                recommendation_accuracy=1,
                safety_violation_rate=0,
                tool_call_count=3,
            ),
            model_name="fake-model",
        )
