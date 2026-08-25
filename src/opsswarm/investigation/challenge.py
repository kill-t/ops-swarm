"""不依赖模型和 Agent 框架的根因假设质疑规则。"""

from collections.abc import Callable

from opsswarm.investigation.models import (
    EvidenceKind,
    EvidenceObservation,
    EvidenceRelation,
    HypothesisChallenge,
    HypothesisEvidenceAssessment,
    HypothesisStatus,
    InvestigationArtifacts,
    RootCause,
)

CHALLENGE_RULE_VERSION = "hypothesis-challenge-v1"

AssessmentRule = Callable[[EvidenceObservation], HypothesisEvidenceAssessment]


def challenge_investigation(artifacts: InvestigationArtifacts) -> HypothesisChallenge:
    """用版本化规则审查主根因，不接受模型自报的支持关系。"""

    conclusion = artifacts.result.conclusion
    rule = _RULES[conclusion.root_cause]
    assessments = tuple(rule(item) for item in artifacts.evidence_items)
    if conclusion.root_cause is RootCause.RELEASE_500_REGRESSION:
        assessments = _challenge_release_versions(artifacts.evidence_items, assessments)
    status = _determine_status(artifacts.evidence_items, assessments)
    return HypothesisChallenge(
        root_cause=conclusion.root_cause,
        statement=conclusion.summary,
        confidence=conclusion.confidence,
        status=status,
        summary=_status_summary(status, assessments),
        rule_version=CHALLENGE_RULE_VERSION,
        evidence_assessments=tuple(sorted(assessments, key=lambda item: item.evidence_id)),
    )


def validate_hypothesis_challenge(
    artifacts: InvestigationArtifacts,
    challenge: HypothesisChallenge,
) -> None:
    """持久化前重新计算，防止处理器伪造或使用过期规则结果。"""

    expected = challenge_investigation(artifacts)
    if challenge != expected:
        raise ValueError("根因假设质疑结果与当前确定性规则不一致")


def _release_rule(evidence: EvidenceObservation) -> HypothesisEvidenceAssessment:
    attributes = evidence.attributes
    if evidence.kind is EvidenceKind.CHANGE:
        if attributes.get("change_type") == "deployment" and attributes.get("completed") is True:
            return _assessment(
                evidence,
                EvidenceRelation.SUPPORTS,
                "release_completed_deployment",
                "错误发生前存在已经完成的版本发布。",
            )
    elif evidence.kind is EvidenceKind.LOG:
        status_code = attributes.get("status_code")
        if (
            isinstance(status_code, int)
            and status_code >= 500
            and attributes.get("release_version")
        ):
            return _assessment(
                evidence,
                EvidenceRelation.SUPPORTS,
                "release_runtime_error",
                "运行时错误来自明确版本并产生服务端错误。",
            )
    elif evidence.kind is EvidenceKind.METRIC:
        if attributes.get("postgres_healthy") is False or attributes.get("redis_healthy") is False:
            return _assessment(
                evidence,
                EvidenceRelation.REFUTES,
                "release_dependency_unhealthy",
                "关键依赖不健康，为版本回归提供了竞争性解释。",
            )
        before = attributes.get("http_5xx_ratio_before")
        after = attributes.get("http_5xx_ratio_after")
        if (
            isinstance(before, int | float)
            and isinstance(after, int | float)
            and after >= 0.05
            and after > before
        ):
            return _assessment(
                evidence,
                EvidenceRelation.SUPPORTS,
                "release_error_rate_spike",
                "服务端错误率显著上升且关键依赖保持健康。",
            )
    return _context(evidence)


def _database_pool_rule(evidence: EvidenceObservation) -> HypothesisEvidenceAssessment:
    attributes = evidence.attributes
    if evidence.kind is EvidenceKind.METRIC:
        if _number_at_least(attributes.get("database_pool_saturation"), 0.9):
            return _assessment(
                evidence,
                EvidenceRelation.SUPPORTS,
                "database_pool_saturated",
                "数据库连接池饱和，支持连接池配置异常。",
            )
        if attributes.get("postgres_healthy") is False:
            return _assessment(
                evidence,
                EvidenceRelation.SUPPORTS,
                "database_unavailable",
                "数据库健康检查失败，支持数据库连接不可用。",
            )
        if attributes.get("postgres_healthy") is True:
            return _assessment(
                evidence,
                EvidenceRelation.REFUTES,
                "database_postgres_healthy",
                "数据库及连接池指标正常，当前证据反驳连接池故障。",
            )
    if evidence.kind is EvidenceKind.LOG:
        searchable = f"{attributes.get('error_type', '')} {evidence.summary}".lower()
        if any(token in searchable for token in ("pool", "connection", "timeout")):
            return _assessment(
                evidence,
                EvidenceRelation.SUPPORTS,
                "database_connection_error",
                "日志包含连接池、连接或超时错误。",
            )
    return _context(evidence)


def _redis_rule(evidence: EvidenceObservation) -> HypothesisEvidenceAssessment:
    attributes = evidence.attributes
    if evidence.kind is EvidenceKind.METRIC:
        if attributes.get("redis_healthy") is False:
            return _assessment(
                evidence,
                EvidenceRelation.SUPPORTS,
                "redis_health_failed",
                "Redis 健康指标明确失败。",
            )
        if attributes.get("redis_healthy") is True:
            return _assessment(
                evidence,
                EvidenceRelation.REFUTES,
                "redis_health_normal",
                "Redis 健康指标正常，反驳 Redis 不可用假设。",
            )
    if evidence.kind is EvidenceKind.LOG:
        searchable = f"{attributes.get('error_type', '')} {evidence.summary}".lower()
        if "redis" in searchable and any(
            token in searchable for token in ("connection", "timeout", "unavailable")
        ):
            return _assessment(
                evidence,
                EvidenceRelation.SUPPORTS,
                "redis_connection_error",
                "日志包含 Redis 连接、超时或不可用错误。",
            )
    return _context(evidence)


def _undetermined_rule(evidence: EvidenceObservation) -> HypothesisEvidenceAssessment:
    return _context(evidence)


def _challenge_release_versions(
    evidence_items: tuple[EvidenceObservation, ...],
    assessments: tuple[HypothesisEvidenceAssessment, ...],
) -> tuple[HypothesisEvidenceAssessment, ...]:
    change_versions = {
        value
        for item in evidence_items
        if item.kind is EvidenceKind.CHANGE
        if isinstance((value := item.attributes.get("to_version")), str)
    }
    log_versions = {
        value
        for item in evidence_items
        if item.kind is EvidenceKind.LOG
        if isinstance((value := item.attributes.get("release_version")), str)
    }
    if not change_versions or not log_versions or change_versions & log_versions:
        return assessments

    evidence_by_id = {item.evidence_id: item for item in evidence_items}
    return tuple(
        _assessment(
            evidence_by_id[item.evidence_id],
            EvidenceRelation.REFUTES,
            "release_version_mismatch",
            "日志中的运行版本与最近发布目标版本不一致。",
        )
        if evidence_by_id[item.evidence_id].kind in {EvidenceKind.LOG, EvidenceKind.CHANGE}
        else item
        for item in assessments
    )


def _determine_status(
    evidence_items: tuple[EvidenceObservation, ...],
    assessments: tuple[HypothesisEvidenceAssessment, ...],
) -> HypothesisStatus:
    if any(item.relation is EvidenceRelation.REFUTES for item in assessments):
        return HypothesisStatus.CONTESTED
    kind_by_id = {item.evidence_id: item.kind for item in evidence_items}
    supporting_kinds = {
        kind_by_id[item.evidence_id]
        for item in assessments
        if item.relation is EvidenceRelation.SUPPORTS
    }
    if len(supporting_kinds) >= 2:
        return HypothesisStatus.SUPPORTED
    return HypothesisStatus.INSUFFICIENT_EVIDENCE


def _status_summary(
    status: HypothesisStatus,
    assessments: tuple[HypothesisEvidenceAssessment, ...],
) -> str:
    support_count = sum(item.relation is EvidenceRelation.SUPPORTS for item in assessments)
    refute_count = sum(item.relation is EvidenceRelation.REFUTES for item in assessments)
    context_count = len(assessments) - support_count - refute_count
    if status is HypothesisStatus.SUPPORTED:
        prefix = "假设通过确定性证据质疑"
    elif status is HypothesisStatus.CONTESTED:
        prefix = "假设存在反驳证据，不能进入处置计划"
    else:
        prefix = "独立支持证据不足，需要继续调查"
    return f"{prefix}；支持 {support_count} 条，反驳 {refute_count} 条，上下文 {context_count} 条。"


def _context(evidence: EvidenceObservation) -> HypothesisEvidenceAssessment:
    return _assessment(
        evidence,
        EvidenceRelation.CONTEXT,
        "no_matching_rule",
        "当前版本规则无法确认该证据支持或反驳主根因。",
    )


def _assessment(
    evidence: EvidenceObservation,
    relation: EvidenceRelation,
    rule_code: str,
    rationale: str,
) -> HypothesisEvidenceAssessment:
    return HypothesisEvidenceAssessment(
        evidence_id=evidence.evidence_id,
        relation=relation,
        rule_code=rule_code,
        rationale=rationale,
    )


def _number_at_least(value: object, threshold: float) -> bool:
    return isinstance(value, int | float) and value >= threshold


_RULES: dict[RootCause, AssessmentRule] = {
    RootCause.RELEASE_500_REGRESSION: _release_rule,
    RootCause.DATABASE_POOL_MISCONFIGURATION: _database_pool_rule,
    RootCause.REDIS_UNAVAILABLE: _redis_rule,
    RootCause.UNDETERMINED: _undetermined_rule,
}
