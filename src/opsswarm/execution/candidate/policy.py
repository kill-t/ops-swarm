"""候选只读前置检查的白名单及确定性阻断规则。"""

from pydantic import ValidationError

from opsswarm.approval.decision.contracts import DecisionRecord
from opsswarm.execution.candidate.contracts import (
    PreflightContent,
    PreflightError,
    RuntimeObservation,
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
)


def validate_step(step: ActionStep) -> None:
    """网络前校验完整参数与目标，不能沿用旧执行表的步骤身份。"""

    try:
        checked = ActionStep.model_validate(step.model_dump(), strict=True)
    except (ValidationError, AttributeError, TypeError) as error:
        raise PreflightError(
            "operation_not_whitelisted", "前置检查的动作或参数不在白名单"
        ) from error
    service = checked.target_type is TargetType.SERVICE and checked.target_name == "order-service"
    redis = checked.target_type is TargetType.DEPENDENCY and checked.target_name == "redis"
    allowed = (
        (
            checked.action_type is ActionType.ROLLBACK_RELEASE
            and isinstance(checked.parameters, RollbackReleaseParameters)
            and service
        )
        or (
            checked.action_type is ActionType.ROLLBACK_CONFIGURATION
            and isinstance(checked.parameters, RollbackConfigurationParameters)
            and checked.parameters.key is ConfigurationKey.DATABASE_POOL_SIZE
            and service
        )
        or (
            checked.action_type is ActionType.RESTORE_DEPENDENCY
            and isinstance(checked.parameters, RestoreDependencyParameters)
            and checked.parameters.dependency is DependencyName.REDIS
            and redis
        )
    )
    if not allowed:
        raise PreflightError("operation_not_whitelisted", "前置检查的动作或目标不在白名单")


def build_content(
    decision: DecisionRecord, step: ActionStep, observation: RuntimeObservation
) -> PreflightContent:
    """参数匹配也不证明自然语言条件成立，更不能直接授予执行资格。"""

    validate_step(step)
    if decision.outcome != "approved" or observation.action_type is not step.action_type:
        raise PreflightError("preflight_source_mismatch", "前置检查必须引用对应动作的精确批准")
    blockers = ["unverified_preconditions"]
    if decision.content.unresolved_questions:
        blockers.append("unresolved_questions")
    if step.action_type is ActionType.ROLLBACK_RELEASE:
        blockers.append("runtime_version_unverifiable")
    if observation.error_code is not None:
        blockers.append("runtime_observation_failed")
    elif isinstance(step.parameters, RollbackConfigurationParameters):
        if observation.current_value == step.parameters.target_value:
            blockers.append("already_at_target")
        elif observation.current_value != step.parameters.current_value:
            blockers.append("configuration_current_value_mismatch")
    elif isinstance(step.parameters, RestoreDependencyParameters):
        if observation.available is step.parameters.target_available:
            blockers.append("already_at_target")
        elif observation.available is not step.parameters.expected_available:
            blockers.append("dependency_current_state_mismatch")
    return PreflightContent(
        decision_id=decision.id,
        decision_content_sha256=decision.content_sha256,
        plan_content_sha256=decision.plan_content_sha256,
        step=step,
        observation=observation,
        preconditions=decision.content.preconditions,
        unresolved_questions=decision.content.unresolved_questions,
        blockers=tuple(blockers),
    )
