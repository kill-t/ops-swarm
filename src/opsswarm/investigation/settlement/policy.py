"""恢复结果到后续意图的确定性映射，不执行重新调查或人工处理。"""

from uuid import UUID

from opsswarm.investigation.settlement.contracts import Disposition, SettlementError
from opsswarm.verification.candidate.contracts import RecoveryResultContent


def settlement_id(run_id: UUID) -> UUID:
    return run_id


def disposition(result: RecoveryResultContent) -> Disposition:
    checked = RecoveryResultContent.model_validate(result)
    if checked.outcome == "recovered":
        if checked.reason_code != "business_probe_passed":
            raise SettlementError("candidate_settlement_integrity_violation", "恢复结果原因不一致")
        return "recovery_verified"
    if checked.outcome == "not_recovered":
        if checked.reason_code == "business_probe_failed":
            return "reinvestigation_required"
        if checked.reason_code != "execution_superseded":
            raise SettlementError(
                "candidate_settlement_integrity_violation", "未恢复结果原因不一致"
            )
    elif checked.outcome == "source_failed":
        if checked.reason_code not in {"probe_source_failed", "recovery_exhausted"}:
            raise SettlementError("candidate_settlement_integrity_violation", "来源失败原因不一致")
    elif checked.outcome == "execution_unknown":
        if checked.reason_code != "execution_unknown":
            raise SettlementError("candidate_settlement_integrity_violation", "未知结果原因不一致")
    elif checked.outcome == "not_executed" and checked.reason_code != "execution_not_applied":
        raise SettlementError("candidate_settlement_integrity_violation", "未执行结果原因不一致")
    return "human_resolution_required"


def require_execution_binding(execution_outcome: str, result: RecoveryResultContent) -> None:
    """窄父状态校验也不能把未知执行改称业务恢复。"""
    expected = {
        "unknown": ("execution_unknown", "execution_unknown"),
        "not_applied": ("not_executed", "execution_not_applied"),
        "blocked": ("not_executed", "execution_not_applied"),
        "applied_superseded": ("not_recovered", "execution_superseded"),
    }
    if execution_outcome == "applied_current":
        valid = (
            result.outcome in {"recovered", "not_recovered", "source_failed"}
            and result.reason_code != "execution_superseded"
        )
    else:
        valid = expected.get(execution_outcome) == (result.outcome, result.reason_code)
    if not valid:
        raise SettlementError("candidate_settlement_integrity_violation", "执行与恢复结论不一致")
