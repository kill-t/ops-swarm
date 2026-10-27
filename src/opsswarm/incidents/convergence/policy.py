"""只根据精确历史样本和当前事故快照编译动作，不产生外部访问。"""

from datetime import datetime, timedelta
from uuid import UUID

from opsswarm.incidents.convergence.contracts import (
    ConvergenceContent,
    ConvergenceError,
    ConvergenceOutcome,
)
from opsswarm.incidents.models import IncidentAction, IncidentStatus
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.settlement.contracts import SettlementRecord
from opsswarm.verification.candidate.contracts import RecoveryView


def compile_convergence(
    settlement: SettlementRecord,
    recovery: RecoveryView,
    *,
    incident_id: UUID,
    status: IncidentStatus,
    version: int,
    latest_alert_id: UUID | None,
    latest_alert_ingested_at: datetime | None,
    now: datetime,
) -> ConvergenceContent:
    result, record = recovery.result, recovery.record
    if (
        result is None
        or record is None
        or record.id != settlement.recovery_job_id
        or record.investigation_run_id != settlement.investigation_run_id
        or record.candidate_job_id != settlement.candidate_job_id
        or record.verification_version != settlement.verification_version
        or record.operation_id != settlement.operation_id
        or record.execution_result_sha256 != settlement.execution_result_sha256
        or result.execution_job_id != record.id
        or result.operation_id != record.operation_id
        or result.execution_result_sha256 != record.execution_result_sha256
        or settlement.recovery_result_sha256 != content_hash(result)
        or settlement.outcome != result.outcome
        or settlement.reason_code != result.reason_code
        or now < settlement.created_at
    ):
        raise ConvergenceError(
            "candidate_convergence_integrity_violation", "收敛缺少精确收尾及恢复事实"
        )
    known_health = result.outcome == "recovered" or (
        result.outcome == "not_recovered" and result.reason_code == "business_probe_failed"
    )
    fresh_until, last_observed = None, None
    if known_health:
        chosen = tuple(s for s in recovery.samples if s.attempt_id == result.attempt_id)
        if (
            len(chosen) != 3
            or tuple(s.sequence for s in chosen) != (1, 2, 3)
            or any(s.observation is None for s in chosen)
        ):
            raise ConvergenceError(
                "candidate_convergence_integrity_violation", "健康结论没有同次三份样本"
            )
        observations = tuple(s.observation for s in chosen if s.observation is not None)
        if any(o.observed_at > now for o in observations):
            raise ConvergenceError("candidate_convergence_clock_invalid", "收敛时刻早于样本")
        fresh_until = min(
            record.window_end, min(o.observed_at for o in observations) + timedelta(seconds=120)
        )
        last_observed = max(o.observed_at for o in observations)
    actions: tuple[IncidentAction, ...] = ()
    after, reason = status, result.reason_code
    outcome: ConvergenceOutcome = "held_uncertain"
    if status != IncidentStatus.MITIGATING or version != settlement.incident_version:
        outcome, reason = "held_incident_changed", "incident_basis_changed"
    elif not known_health:
        pass
    elif fresh_until is None or now >= fresh_until:
        outcome, reason = "held_stale", "recovery_samples_stale"
    elif (
        latest_alert_ingested_at is not None
        and last_observed is not None
        and latest_alert_ingested_at > last_observed
    ):
        outcome, reason = "held_new_alert", "new_alert_after_samples"
    elif result.outcome == "recovered":
        outcome, reason, after = "recovered", "fresh_recovery_verified", IncidentStatus.RESOLVED
        actions = (IncidentAction.COMPLETE_MITIGATION, IncidentAction.CONFIRM_RECOVERY)
    else:
        outcome, reason, after = (
            "observed_failure",
            "fresh_verification_failed",
            IncidentStatus.INVESTIGATING,
        )
        actions = (IncidentAction.COMPLETE_MITIGATION, IncidentAction.FAIL_VERIFICATION)
    return ConvergenceContent(
        investigation_run_id=settlement.investigation_run_id,
        incident_id=incident_id,
        settlement_content_sha256=settlement.content_sha256,
        before_status=status,
        before_version=version,
        after_status=after,
        after_version=version + len(actions),
        outcome=outcome,
        reason_code=reason,
        fresh_until=fresh_until,
        latest_alert_id=latest_alert_id,
        latest_alert_ingested_at=latest_alert_ingested_at,
        actions=actions,
        slot_released=outcome == "recovered",
        created_at=now,
    )
