"""时效、告警和未知分支不能被历史收尾时刻或结果名称绕过。"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from opsswarm.incidents.convergence.contracts import ConvergenceContent, ConvergenceError
from opsswarm.incidents.convergence.policy import compile_convergence
from opsswarm.incidents.models import IncidentAction, IncidentStatus
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.settlement.contracts import SettlementContent, SettlementRecord
from opsswarm.verification.candidate.contracts import (
    ProbeObservation,
    RecoveryJobRecord,
    RecoveryResultContent,
    RecoverySampleRecord,
    RecoveryView,
)

NOW = datetime(2026, 10, 27, tzinfo=UTC)


def sources(outcome="recovered", reason="business_probe_passed"):
    run_id, job_id, candidate_id, operation_id, attempt_id = (uuid4() for _ in range(5))
    known = outcome == "recovered" or reason == "business_probe_failed"
    samples = (
        tuple(
            RecoverySampleRecord(
                job_id=job_id,
                attempt_id=attempt_id,
                sequence=index + 1,
                observation=ProbeObservation(
                    requested_at=NOW - timedelta(seconds=10 - 2 * index),
                    observed_at=NOW - timedelta(seconds=10 - 2 * index),
                    http_status=500 if outcome == "not_recovered" and index == 1 else 200,
                    value=1.0 if outcome == "not_recovered" and index == 1 else 0.0,
                    order_id=None
                    if outcome == "not_recovered" and index == 1
                    else "opsswarm-verification-probe",
                ),
                error_code=None,
                created_at=NOW - timedelta(seconds=10 - 2 * index),
            )
            for index in range(3)
        )
        if known
        else ()
    )
    result = RecoveryResultContent(
        execution_job_id=job_id,
        execution_result_sha256="a" * 64,
        operation_id=operation_id,
        outcome=outcome,
        attempt_id=attempt_id if known else None,
        reason_code=reason,
        created_at=NOW - timedelta(seconds=5),
        parent_advanced=False,
    )
    record = RecoveryJobRecord(
        id=job_id,
        investigation_run_id=run_id,
        candidate_job_id=candidate_id,
        verification_version=1,
        operation_id=operation_id,
        execution_result_sha256="a" * 64,
        window_start=NOW - timedelta(seconds=11),
        window_end=NOW + timedelta(seconds=109),
        status="completed",
        attempt_count=1 if known else 0,
        created_at=NOW - timedelta(seconds=11),
        updated_at=result.created_at,
        completed_at=result.created_at,
    )
    content = SettlementContent(
        id=run_id,
        investigation_run_id=run_id,
        recovery_job_id=job_id,
        candidate_job_id=candidate_id,
        verification_version=1,
        operation_id=operation_id,
        execution_result_sha256="a" * 64,
        recovery_result_sha256=content_hash(result),
        handoff_id=uuid4(),
        previous_run_status="await_candidate",
        previous_run_updated_at=NOW - timedelta(seconds=50),
        incident_version=4,
        outcome=outcome,
        disposition="recovery_verified" if outcome == "recovered" else "human_resolution_required",
        reason_code=reason,
        event_cursor=10,
        created_at=NOW - timedelta(seconds=4),
    )
    return (
        SettlementRecord(**content.model_dump(), content_sha256=content_hash(content)),
        RecoveryView(record=record, attempts=(), samples=samples, result=result),
    )


def decide(settlement, recovery, **changes):
    options = dict(
        incident_id=uuid4(),
        status=IncidentStatus.MITIGATING,
        version=4,
        latest_alert_id=None,
        latest_alert_ingested_at=None,
        now=NOW,
    )
    options.update(changes)
    return compile_convergence(settlement, recovery, **options)


@pytest.mark.parametrize("failed", [False, True])
def test_actual_business_outcome_uses_two_legal_hops_and_only_success_releases(failed):
    settlement, recovery = sources(
        "not_recovered" if failed else "recovered",
        "business_probe_failed" if failed else "business_probe_passed",
    )
    chosen = decide(settlement, recovery)
    assert chosen.actions == (
        IncidentAction.COMPLETE_MITIGATION,
        IncidentAction.FAIL_VERIFICATION if failed else IncidentAction.CONFIRM_RECOVERY,
    )
    assert chosen.after_status == (
        IncidentStatus.INVESTIGATING if failed else IncidentStatus.RESOLVED
    )
    assert chosen.after_version == 6 and chosen.slot_released is (not failed)


@pytest.mark.parametrize(
    ("outcome", "reason"),
    [
        ("source_failed", "probe_source_failed"),
        ("source_failed", "recovery_exhausted"),
        ("execution_unknown", "execution_unknown"),
        ("not_executed", "execution_not_applied"),
        ("not_recovered", "execution_superseded"),
    ],
)
def test_uncertainty_and_non_execution_are_not_failure_or_recovery(outcome, reason):
    chosen = decide(*sources(outcome, reason))
    assert chosen.outcome == "held_uncertain" and chosen.reason_code == reason
    assert chosen.actions == () and chosen.after_status == IncidentStatus.MITIGATING
    assert chosen.after_version == 4 and chosen.slot_released is False
    assert chosen.fresh_until is None


@pytest.mark.parametrize(
    ("delta", "expected"), [(-1, "recovered"), (0, "held_stale"), (1, "held_stale")]
)
def test_fixed_window_deadline_is_exclusive(delta, expected):
    settlement, recovery = sources()
    deadline = recovery.record.window_end
    chosen = decide(settlement, recovery, now=deadline + timedelta(microseconds=delta))
    assert chosen.fresh_until == deadline and chosen.outcome == expected


def test_earliest_observation_limits_age_even_if_window_is_later():
    settlement, recovery = sources()
    recovery = recovery.model_copy(
        update={
            "record": recovery.record.model_copy(
                update={"window_end": NOW + timedelta(seconds=500)}
            )
        }
    )
    chosen = decide(settlement, recovery, now=NOW + timedelta(seconds=110))
    assert chosen.outcome == "held_stale" and chosen.fresh_until == NOW + timedelta(seconds=110)


@pytest.mark.parametrize("delta", [-1, 0, 1])
def test_alert_after_last_observation_blocks_health_assertion(delta):
    settlement, recovery = sources()
    chosen = decide(
        settlement,
        recovery,
        latest_alert_id=uuid4(),
        latest_alert_ingested_at=NOW - timedelta(seconds=6) + timedelta(microseconds=delta),
    )
    assert chosen.outcome == ("held_new_alert" if delta > 0 else "recovered")


@pytest.mark.parametrize(
    ("status", "version"), [(IncidentStatus.VERIFYING, 4), (IncidentStatus.MITIGATING, 5)]
)
def test_changed_incident_takes_precedence_over_a_valid_recovery(status, version):
    chosen = decide(*sources(), status=status, version=version)
    assert chosen.outcome == "held_incident_changed" and chosen.actions == ()
    assert chosen.after_version == version and chosen.slot_released is False


@pytest.mark.parametrize(
    "field",
    [
        "id",
        "investigation_run_id",
        "candidate_job_id",
        "operation_id",
        "execution_result_sha256",
        "verification_version",
    ],
)
def test_mismatched_recovery_scope_is_not_downgraded_to_unknown(field):
    settlement, recovery = sources()
    value = (
        "f" * 64 if field.endswith("sha256") else 2 if field == "verification_version" else uuid4()
    )
    recovery = recovery.model_copy(
        update={"record": recovery.record.model_copy(update={field: value})}
    )
    with pytest.raises(ConvergenceError):
        decide(settlement, recovery)


def test_samples_cannot_be_borrowed_from_another_attempt():
    settlement, recovery = sources()
    first = recovery.samples[0].model_copy(update={"attempt_id": uuid4()})
    recovery = recovery.model_copy(update={"samples": (first, *recovery.samples[1:])})
    with pytest.raises(ConvergenceError):
        decide(settlement, recovery)


def test_settlement_time_does_not_refresh_samples():
    settlement, recovery = sources()
    settlement = settlement.model_copy(update={"created_at": NOW + timedelta(seconds=119)})
    assert decide(settlement, recovery, now=NOW + timedelta(seconds=120)).outcome == "held_stale"


def test_clock_cannot_precede_settlement():
    with pytest.raises(ConvergenceError):
        decide(*sources(), now=NOW - timedelta(seconds=5))


def test_release_flag_does_not_accept_numeric_boolean():
    data = decide(*sources()).model_dump()
    data["slot_released"] = 1
    with pytest.raises(ValidationError):
        ConvergenceContent.model_validate(data)
