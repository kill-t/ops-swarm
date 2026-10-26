"""精确批准、发布白名单、只读观测与原时限的确定性准备规则。"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from opsswarm.approval.conditions.contracts import DecisionRecord
from opsswarm.execution.release.contracts import (
    ReleaseObservation,
    ReleaseRequestAvailability,
    ReleaseRequestCommand,
    ReleaseRequestContent,
    ReleaseRequestError,
)
from opsswarm.fault_lab.release.contracts import ReleaseCommand
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.planning.candidate.contracts import PlanRecord
from opsswarm.planning.models import ActionStep, ActionType, RollbackReleaseParameters, TargetType


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("发布准备时间必须包含时区")
    try:
        return value.astimezone(UTC)
    except OverflowError as error:
        raise ValueError("发布准备时间归一UTC后越界") from error


def validate_release_step(step: ActionStep) -> None:
    try:
        checked = ActionStep.model_validate(step.model_dump(), strict=True)
    except (ValueError, AttributeError, TypeError) as error:
        raise ReleaseRequestError(
            "candidate_release_step_invalid", "发布准备步骤无法完整校验"
        ) from error
    if (
        checked.action_type is not ActionType.ROLLBACK_RELEASE
        or checked.target_type is not TargetType.SERVICE
        or checked.target_name != "order-service"
        or not isinstance(checked.parameters, RollbackReleaseParameters)
        or checked.parameters.current_version != "2026.01.01-rc1"
        or checked.parameters.target_version != "stable"
    ):
        raise ReleaseRequestError(
            "candidate_release_not_whitelisted", "仅允许本地order-service的rc1到stable发布回滚"
        )


def _validate_approval(approval: DecisionRecord) -> None:
    content = approval.content
    if (
        approval.outcome != "approved"
        or content.outcome != "approved"
        or approval.content_sha256 != content_hash(content)
        or content.execution_eligible is not False
        or content.conditions_status != "ready_for_review"
        or content.request_id != approval.request_id
        or content.request_content_sha256 != approval.request_content_sha256
        or content.verification_id != approval.verification_id
        or content.verification_version != approval.verification_version
        or content.verification_content_sha256 != approval.verification_content_sha256
    ):
        raise ReleaseRequestError(
            "candidate_release_approval_invalid", "发布准备缺少精确完整核验批准"
        )


def validate_release_plan(plan: PlanRecord) -> ActionStep:
    if (
        content_hash(plan.content) != plan.content_sha256
        or plan.content.execution_eligible is not False
        or plan.content.approval_eligible is not False
        or len(plan.content.plan.steps) != 1
    ):
        raise ReleaseRequestError("candidate_release_plan_mismatch", "发布准备缺少完整唯一原计划")
    try:
        checked = PlanRecord.model_validate(plan.model_dump())
    except ValueError as error:
        raise ReleaseRequestError("candidate_release_plan_invalid", "原计划无法完整校验") from error
    step = checked.content.plan.steps[0]
    validate_release_step(step)
    return step


def build_content(
    approval: DecisionRecord,
    plan: PlanRecord,
    observation: ReleaseObservation,
    operation_id: UUID,
) -> ReleaseRequestContent:
    _validate_approval(approval)
    if (
        plan.id != approval.content.plan_id
        or plan.content_sha256 != approval.content.plan_content_sha256
        or content_hash(plan.content) != plan.content_sha256
        or plan.investigation_run_id != approval.investigation_run_id
        or plan.candidate_job_id != approval.candidate_job_id
        or plan.content.execution_eligible is not False
        or plan.content.approval_eligible is not False
        or len(plan.content.plan.steps) != 1
    ):
        raise ReleaseRequestError(
            "candidate_release_plan_mismatch", "发布准备未绑定批准中的唯一原计划"
        )
    step = validate_release_plan(plan)
    try:
        checked_observation = ReleaseObservation.model_validate(observation.model_dump())
    except ValueError as error:
        raise ReleaseRequestError(
            "candidate_release_source_invalid", "发布准备计划或观测结构不完整"
        ) from error
    state = checked_observation.state
    if state.version != "2026.01.01-rc1" or state.release_500_enabled is not True:
        raise ReleaseRequestError(
            "candidate_release_current_version_mismatch", "当前演练版本不等于原计划rc1"
        )
    return ReleaseRequestContent(
        approval_decision_id=approval.id,
        approval_content_sha256=approval.content_sha256,
        verification_id=approval.verification_id,
        verification_version=approval.verification_version,
        verification_content_sha256=approval.verification_content_sha256,
        plan_id=plan.id,
        plan_content_sha256=plan.content_sha256,
        operation_id=operation_id,
        step=step,
        observation=checked_observation,
        cas_command=ReleaseCommand(
            expected_version="2026.01.01-rc1",
            expected_revision=state.revision,
            target_version="stable",
            idempotency_key=f"candidate-release-{operation_id}",
        ),
    )


def validate_command(
    command: ReleaseRequestCommand,
    approval: DecisionRecord,
    *,
    availability: ReleaseRequestAvailability,
    now: datetime,
    expires_at: datetime,
) -> None:
    _validate_approval(approval)
    if (
        command.approval_decision_id != approval.id
        or command.approval_content_sha256 != approval.content_sha256
    ):
        raise ReleaseRequestError("candidate_release_approval_mismatch", "命令未精确绑定核验批准")
    if command.expected_incident_version != approval.incident_version:
        raise ReleaseRequestError(
            "candidate_release_incident_version_conflict", "命令未绑定原核验批准的事故版本"
        )
    current = _utc(now)
    if current < _utc(approval.created_at):
        raise ReleaseRequestError("candidate_release_before_approval", "准备时间不能早于原核验批准")
    if current >= _utc(expires_at):
        raise ReleaseRequestError("candidate_release_expired", "原核验批准期限已到")
    if availability != "current":
        raise ReleaseRequestError("candidate_release_source_stale", "原核验批准已失效或被替代")


def expires_at(
    original_deadline: datetime,
    observation: ReleaseObservation,
    created_at: datetime,
) -> datetime:
    now, observed = _utc(created_at), _utc(observation.observed_at)
    try:
        deadline = min(_utc(original_deadline), observed + timedelta(seconds=30))
    except OverflowError as error:
        raise ReleaseRequestError(
            "candidate_release_observation_invalid", "观测有效期越界"
        ) from error
    if observed > now:
        raise ReleaseRequestError(
            "candidate_release_observation_future", "观测时间不能晚于本次准备"
        )
    if now >= deadline:
        raise ReleaseRequestError("candidate_release_observation_stale", "只读观测或原批准已到期")
    return deadline
