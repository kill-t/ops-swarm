"""以原结构化证据构造白名单步骤、参数、风险与验证规则。"""

import re
from collections.abc import Sequence
from typing import cast

from opsswarm.investigation.models import EvidenceKind, EvidenceObservation
from opsswarm.planning.errors import (
    UnsafeConfigurationRollbackTarget,
    UnsafeDependencyRestoreTarget,
    UnsafeRollbackTarget,
)
from opsswarm.planning.models import (
    ActionStep,
    ActionType,
    ConfigurationKey,
    DependencyName,
    RestoreDependencyParameters,
    RollbackConfigurationParameters,
    RollbackReleaseParameters,
    TargetType,
    VerificationOperator,
    VerificationRule,
)
from opsswarm.planning.policy import classify_action_risk


def build_candidate_step(
    service_name: str,
    action_type: ActionType,
    evidence_items: Sequence[EvidenceObservation],
) -> tuple[ActionStep, str]:
    """模板可行性不赋予根因支持状态，也不创建计划或授权。"""
    if action_type is ActionType.ROLLBACK_RELEASE:
        return _release_rollback_step(service_name, evidence_items)
    if action_type is ActionType.ROLLBACK_CONFIGURATION:
        return _configuration_rollback_step(service_name, evidence_items)
    if action_type is ActionType.RESTORE_DEPENDENCY:
        return _redis_restore_step(evidence_items)
    raise ValueError("未知白名单能力")


def _release_rollback_step(
    service_name: str,
    evidence_items: Sequence[EvidenceObservation],
) -> tuple[ActionStep, str]:
    change = _latest_safe_deployment(evidence_items)
    parameters = RollbackReleaseParameters(
        current_version=str(change.attributes["to_version"]),
        target_version=str(change.attributes["from_version"]),
    )
    risk = classify_action_risk(ActionType.ROLLBACK_RELEASE, parameters)
    step = ActionStep(
        step_order=1,
        action_type=ActionType.ROLLBACK_RELEASE,
        target_type=TargetType.SERVICE,
        target_name=service_name,
        parameters=parameters,
        risk_level=risk.risk_level,
        requires_human_approval=risk.requires_human_approval,
        risk_rule_code=risk.rule_code,
        risk_rationale=risk.rationale,
        verification=VerificationRule(
            signal="http_5xx_ratio",
            operator=VerificationOperator.LESS_THAN,
            threshold=0.01,
            observation_window_seconds=60,
            required_successful_samples=3,
        ),
    )
    return (
        step,
        (
            f"将 {service_name} 从 {parameters.current_version} 回滚到 "
            f"{parameters.target_version}，随后验证 5xx 比例持续低于 1%。"
        ),
    )


def _configuration_rollback_step(
    service_name: str,
    evidence_items: Sequence[EvidenceObservation],
) -> tuple[ActionStep, str]:
    change = _latest_safe_database_pool_change(evidence_items)
    parameters = RollbackConfigurationParameters(
        key=ConfigurationKey.DATABASE_POOL_SIZE,
        current_value=cast(int, change.attributes["to_value"]),
        target_value=cast(int, change.attributes["from_value"]),
    )
    risk = classify_action_risk(ActionType.ROLLBACK_CONFIGURATION, parameters)
    step = ActionStep(
        step_order=1,
        action_type=ActionType.ROLLBACK_CONFIGURATION,
        target_type=TargetType.SERVICE,
        target_name=service_name,
        parameters=parameters,
        risk_level=risk.risk_level,
        requires_human_approval=risk.requires_human_approval,
        risk_rule_code=risk.rule_code,
        risk_rationale=risk.rationale,
        verification=VerificationRule(
            signal="database_pool_saturation",
            operator=VerificationOperator.LESS_THAN,
            threshold=0.8,
            observation_window_seconds=60,
            required_successful_samples=3,
        ),
    )
    return (
        step,
        (
            f"将 {service_name} 的 {parameters.key.value} 从 {parameters.current_value} "
            f"回滚到 {parameters.target_value}，随后验证连接池饱和度持续低于 80%。"
        ),
    )


def _redis_restore_step(
    evidence_items: Sequence[EvidenceObservation],
) -> tuple[ActionStep, str]:
    if not any(
        item.kind is EvidenceKind.METRIC and item.attributes.get("redis_healthy") is False
        for item in evidence_items
    ):
        raise UnsafeDependencyRestoreTarget()
    parameters = RestoreDependencyParameters(
        dependency=DependencyName.REDIS,
        expected_available=False,
        target_available=True,
    )
    risk = classify_action_risk(ActionType.RESTORE_DEPENDENCY, parameters)
    step = ActionStep(
        step_order=1,
        action_type=ActionType.RESTORE_DEPENDENCY,
        target_type=TargetType.DEPENDENCY,
        target_name=DependencyName.REDIS.value,
        parameters=parameters,
        risk_level=risk.risk_level,
        requires_human_approval=risk.requires_human_approval,
        risk_rule_code=risk.rule_code,
        risk_rationale=risk.rationale,
        verification=VerificationRule(
            signal="redis_unavailable_ratio",
            operator=VerificationOperator.LESS_THAN,
            threshold=0.01,
            observation_window_seconds=60,
            required_successful_samples=3,
        ),
    )
    return (
        step,
        "将本地 Redis 依赖从不可用恢复为可用，随后验证不可用比例持续低于 1%。",
    )


def _latest_safe_deployment(
    evidence_items: Sequence[EvidenceObservation],
) -> EvidenceObservation:
    candidates = [
        item
        for item in evidence_items
        if item.kind is EvidenceKind.CHANGE
        and item.attributes.get("change_type") == "deployment"
        and item.attributes.get("completed") is True
        and _valid_version(item.attributes.get("from_version"))
        and _valid_version(item.attributes.get("to_version"))
        and item.attributes["from_version"] != item.attributes["to_version"]
    ]
    if not candidates:
        raise UnsafeRollbackTarget()
    return max(candidates, key=lambda item: item.observed_at)


def _valid_version(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) <= 100
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value) is not None
    )


def _latest_safe_database_pool_change(
    evidence_items: Sequence[EvidenceObservation],
) -> EvidenceObservation:
    candidates = [
        item
        for item in evidence_items
        if item.kind is EvidenceKind.CHANGE
        and item.attributes.get("change_type") == "configuration"
        and item.attributes.get("key") == ConfigurationKey.DATABASE_POOL_SIZE.value
        and item.attributes.get("completed") is True
        and _valid_pool_size(item.attributes.get("from_value"))
        and _valid_pool_size(item.attributes.get("to_value"))
        and item.attributes["from_value"] != item.attributes["to_value"]
    ]
    if not candidates:
        raise UnsafeConfigurationRollbackTarget()
    return max(candidates, key=lambda item: item.observed_at)


def _valid_pool_size(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 1000
