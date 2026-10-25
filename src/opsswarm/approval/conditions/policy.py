"""绑定完整核验、原始期限及负面收敛的纯审批规则。"""

from datetime import UTC, datetime

from opsswarm.approval.conditions.contracts import (
    ApprovalAvailability,
    ApprovalError,
    DecisionAction,
    DecisionCommand,
    DecisionContent,
    DecisionOutcome,
    RequestCommand,
    RequestContent,
    RequestRecord,
)
from opsswarm.execution.conditions.contracts import ConditionRecord
from opsswarm.investigation.context.hashing import content_hash


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("审批时间必须包含时区")
    return value.astimezone(UTC)


def build_request_content(verification: ConditionRecord) -> RequestContent:
    """人工核验必须全部verified，审批绑定原事实而不修改原授权标志。"""

    content = verification.content
    if (
        verification.content_sha256 != content_hash(content)
        or verification.decision_id != content.decision_id
        or verification.decision_content_sha256 != content.decision_content_sha256
        or content.approval_required is not True
        or content.execution_eligible is not False
    ):
        raise ApprovalError("condition_approval_source_invalid", "核验内容摘要或原决定引用不一致")
    if content.status != "ready_for_review" or any(
        item.status != "verified" for item in content.items
    ):
        raise ApprovalError(
            "condition_approval_not_ready", "只有全部verified的完整核验可以申请审批"
        )
    return RequestContent(
        verification_id=verification.id,
        verification_version=verification.version,
        verification_content_sha256=verification.content_sha256,
        base_decision_id=content.decision_id,
        base_decision_content_sha256=content.decision_content_sha256,
        plan_id=content.plan_id,
        plan_content_sha256=content.plan_content_sha256,
    )


def validate_request(
    command: RequestCommand,
    verification: ConditionRecord,
    *,
    availability: ApprovalAvailability,
    now: datetime,
) -> None:
    """请求只接受当前精确核验，不延长核验及原批准期限。"""

    build_request_content(verification)
    if (
        command.verification_id != verification.id
        or command.verification_content_sha256 != verification.content_sha256
    ):
        raise ApprovalError("condition_approval_verification_mismatch", "命令未精确绑定原核验")
    if command.expected_incident_version != verification.incident_version:
        raise ApprovalError(
            "condition_approval_incident_version_conflict", "事故版本与原核验不一致"
        )
    current_time = _utc(now)
    if current_time < _utc(verification.created_at):
        raise ApprovalError("condition_approval_before_verification", "请求时间不能早于核验")
    if current_time >= _utc(verification.expires_at):
        raise ApprovalError("condition_approval_expired", "原核验已到期")
    if availability != "current":
        raise ApprovalError("condition_approval_source_stale", "原核验已失效或被替代")


def _validate_request_content(request: RequestRecord) -> None:
    content = request.content
    if (
        request.content_sha256 != content_hash(content)
        or request.verification_id != content.verification_id
        or request.verification_content_sha256 != content.verification_content_sha256
        or request.verification_version != content.verification_version
        or content.conditions_status != "ready_for_review"
        or content.execution_eligible is not False
        or request.status != "pending"
    ):
        raise ApprovalError(
            "condition_approval_request_invalid", "审批请求内容摘要或原核验引用不一致"
        )


def build_decision_content(request: RequestRecord, action: DecisionAction) -> DecisionContent:
    """保留请求的全部精确引用；独立决定始终不提供执行资格。"""

    _validate_request_content(request)
    outcomes: dict[DecisionAction, DecisionOutcome] = {
        "approve": "approved",
        "reject": "rejected",
        "expire": "expired",
    }
    if action not in outcomes:
        raise ValueError("不支持的审批动作")
    return DecisionContent(
        **request.content.model_dump(exclude={"version"}),
        request_id=request.id,
        request_content_sha256=request.content_sha256,
        outcome=outcomes[action],
    )


def validate_decision(
    command: DecisionCommand,
    request: RequestRecord,
    *,
    availability: ApprovalAvailability,
    now: datetime,
) -> None:
    """负面决定仅收敛原请求，不依赖当前事故阶段或剩余预算。"""

    _validate_request_content(request)
    if command.request_id != request.id or command.request_content_sha256 != request.content_sha256:
        raise ApprovalError("condition_approval_request_mismatch", "命令未精确绑定原审批请求")
    if command.expected_incident_version != request.incident_version:
        raise ApprovalError(
            "condition_approval_incident_version_conflict", "命令未绑定请求中的原事故版本"
        )
    validate_timing(command.action, request, now)
    if command.action == "approve" and availability != "current":
        raise ApprovalError("condition_approval_source_stale", "原核验已失效或被替代，不能批准")


def validate_timing(action: DecisionAction, request: RequestRecord, now: datetime) -> None:
    """写后复查仍使用原请求截止时间，不创建新的有效期限。"""

    current_time, deadline = _utc(now), _utc(request.expires_at)
    if current_time < _utc(request.created_at):
        raise ApprovalError("condition_approval_before_request", "决定时间不能早于审批请求")
    if action == "expire":
        if current_time < deadline:
            raise ApprovalError("condition_approval_not_expired", "原审批请求尚未到期")
    elif action in {"approve", "reject"}:
        if current_time >= deadline:
            raise ApprovalError(
                "condition_approval_expired", "原审批请求已到期，必须显式提交过期命令"
            )
    else:
        raise ValueError("不支持的审批动作")


def request_availability(
    source_availability: ApprovalAvailability, has_decision: bool
) -> ApprovalAvailability:
    return "recorded" if has_decision else source_availability


def decision_availability(
    outcome: DecisionOutcome, source_availability: ApprovalAvailability
) -> ApprovalAvailability:
    return source_availability if outcome == "approved" else "recorded"
