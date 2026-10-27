"""在选取前过滤永久失格来源，避免有限扫描被陈旧记录占满。"""

from sqlalchemy import Select, and_, exists, func, or_, select
from sqlalchemy.orm import aliased

from opsswarm.execution.conditions.tables import CandidateConditionRow
from opsswarm.execution.release.tables import CandidateReleaseRequestRow
from opsswarm.execution.release_jobs.tables import ReleaseExecutionJobRow
from opsswarm.incidents.persistence.tables import IncidentRow
from opsswarm.investigation.budget.tables import CallReservationRow
from opsswarm.investigation.candidate.tables import CandidateJobRow
from opsswarm.investigation.human_wait.tables import HumanWaitEventRow
from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.verification.candidate.tables import RecoveryJobRow


def eligible_recoveries() -> Select[tuple[RecoveryJobRow]]:
    highest = (
        select(func.max(CandidateConditionRow.version))
        .where(
            CandidateConditionRow.candidate_job_id == CandidateJobRow.id,
            CandidateConditionRow.investigation_run_id == InvestigationRunRow.id,
        )
        .correlate(CandidateJobRow, InvestigationRunRow)
        .scalar_subquery()
    )
    latest_wait = (
        select(func.max(HumanWaitEventRow.state_version))
        .where(
            HumanWaitEventRow.investigation_run_id == InvestigationRunRow.id,
        )
        .correlate(InvestigationRunRow)
        .scalar_subquery()
    )
    no_cancel = ~exists(
        select(HumanWaitEventRow.id)
        .where(
            HumanWaitEventRow.investigation_run_id == InvestigationRunRow.id,
            HumanWaitEventRow.action == "cancel",
        )
        .correlate(InvestigationRunRow)
    )
    statement = (
        select(RecoveryJobRow)
        .join(InvestigationRunRow, InvestigationRunRow.id == RecoveryJobRow.investigation_run_id)
        .join(CandidateJobRow, CandidateJobRow.id == RecoveryJobRow.candidate_job_id)
        .join(ReleaseExecutionJobRow, ReleaseExecutionJobRow.id == RecoveryJobRow.id)
        .join(
            CandidateReleaseRequestRow,
            CandidateReleaseRequestRow.id == ReleaseExecutionJobRow.request_id,
        )
        .join(IncidentRow, IncidentRow.id == InvestigationRunRow.incident_id)
        .where(
            RecoveryJobRow.status == "completed",
            InvestigationRunRow.status.in_(("await_candidate", "await_human")),
            IncidentRow.status == "mitigating",
            IncidentRow.version == CandidateReleaseRequestRow.incident_version,
            highest == RecoveryJobRow.verification_version,
            no_cancel,
            or_(
                and_(
                    CandidateJobRow.source_kind == "terminal_handoff",
                    InvestigationRunRow.status == "await_candidate",
                ),
                and_(
                    CandidateJobRow.source_kind == "human_route",
                    InvestigationRunRow.status == "await_human",
                    latest_wait == CandidateJobRow.wait_version,
                ),
            ),
        )
    )
    call = aliased(CallReservationRow)
    candidate = aliased(CandidateJobRow)
    execution = aliased(ReleaseExecutionJobRow)
    recovery = aliased(RecoveryJobRow)
    pending_queries = (
        select(call.id).where(
            call.investigation_run_id == InvestigationRunRow.id, call.status == "reserved"
        ),
        select(candidate.id).where(
            candidate.investigation_run_id == InvestigationRunRow.id,
            candidate.status.in_(("queued", "running")),
        ),
        select(execution.id).where(
            execution.investigation_run_id == InvestigationRunRow.id,
            execution.status.in_(("queued", "running")),
        ),
        select(recovery.id).where(
            recovery.investigation_run_id == InvestigationRunRow.id, recovery.status != "completed"
        ),
    )
    for pending in pending_queries:
        statement = statement.where(~exists(pending.correlate(InvestigationRunRow)))
    return statement
