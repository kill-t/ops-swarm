"""使用单条 PostgreSQL 查询构建 Incident 作战室只读投影。"""

from typing import cast
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.approval import ApprovalRequestStatus
from opsswarm.approval.persistence.tables import ApprovalRequestRow
from opsswarm.execution import ExecutionStatus
from opsswarm.execution.persistence.tables import OperationExecutionRow
from opsswarm.incidents import IncidentNotFound
from opsswarm.incidents.persistence.tables import IncidentRow
from opsswarm.investigation.models import (
    HypothesisStatus,
    InvestigationDataSourceMode,
    InvestigationMode,
    InvestigationRunStatus,
    RootCause,
)
from opsswarm.investigation.persistence.tables import (
    EvidenceItemRow,
    HypothesisRow,
    InvestigationRunRow,
    SpecialistReportRow,
)
from opsswarm.planning import ActionPlanStatus, RiskLevel
from opsswarm.planning.persistence.tables import ActionPlanRow, ActionStepRow
from opsswarm.verification import VerificationStatus
from opsswarm.verification.persistence.tables import VerificationRunRow, VerificationSampleRow
from opsswarm.workroom.models import (
    WorkroomActionPlanSummary,
    WorkroomApprovalSummary,
    WorkroomEvidenceBoardSummary,
    WorkroomExecutionSummary,
    WorkroomHypothesisSummary,
    WorkroomInvestigationSummary,
    WorkroomSnapshot,
    WorkroomVerificationSummary,
)


class WorkroomStore:
    """跨领域读取最新处置链路，不提供任何写入方法。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_snapshot(self, incident_id: UUID) -> WorkroomSnapshot:
        """用单条语句读取同一 PostgreSQL 快照中的全部阶段。"""

        latest_run_id = (
            select(InvestigationRunRow.id)
            .where(InvestigationRunRow.incident_id == IncidentRow.id)
            .order_by(InvestigationRunRow.created_at.desc(), InvestigationRunRow.id.desc())
            .limit(1)
            .correlate(IncidentRow)
            .scalar_subquery()
        )
        current_plan_id = (
            select(ActionPlanRow.id)
            .where(ActionPlanRow.investigation_run_id == InvestigationRunRow.id)
            .order_by(ActionPlanRow.version.desc(), ActionPlanRow.id.desc())
            .limit(1)
            .correlate(InvestigationRunRow)
            .scalar_subquery()
        )
        latest_execution_id = (
            select(OperationExecutionRow.id)
            .where(OperationExecutionRow.action_plan_id == ActionPlanRow.id)
            .order_by(
                OperationExecutionRow.created_at.desc(),
                OperationExecutionRow.id.desc(),
            )
            .limit(1)
            .correlate(ActionPlanRow)
            .scalar_subquery()
        )
        evidence_count = (
            select(func.count(EvidenceItemRow.id))
            .where(EvidenceItemRow.investigation_run_id == InvestigationRunRow.id)
            .correlate(InvestigationRunRow)
            .scalar_subquery()
        )
        report_count = (
            select(func.count(SpecialistReportRow.id))
            .where(SpecialistReportRow.investigation_run_id == InvestigationRunRow.id)
            .correlate(InvestigationRunRow)
            .scalar_subquery()
        )
        step_count = (
            select(func.count(ActionStepRow.id))
            .where(ActionStepRow.action_plan_id == ActionPlanRow.id)
            .correlate(ActionPlanRow)
            .scalar_subquery()
        )
        sample_count = (
            select(func.count(VerificationSampleRow.id))
            .where(VerificationSampleRow.verification_run_id == VerificationRunRow.id)
            .correlate(VerificationRunRow)
            .scalar_subquery()
        )
        statement = (
            select(
                IncidentRow,
                InvestigationRunRow,
                HypothesisRow,
                ActionPlanRow,
                ApprovalRequestRow,
                OperationExecutionRow,
                VerificationRunRow,
                evidence_count.label("evidence_count"),
                report_count.label("report_count"),
                step_count.label("step_count"),
                sample_count.label("sample_count"),
            )
            .select_from(IncidentRow)
            .outerjoin(InvestigationRunRow, InvestigationRunRow.id == latest_run_id)
            .outerjoin(
                HypothesisRow,
                HypothesisRow.investigation_run_id == InvestigationRunRow.id,
            )
            .outerjoin(ActionPlanRow, ActionPlanRow.id == current_plan_id)
            .outerjoin(
                ApprovalRequestRow,
                ApprovalRequestRow.action_plan_id == ActionPlanRow.id,
            )
            .outerjoin(OperationExecutionRow, OperationExecutionRow.id == latest_execution_id)
            .outerjoin(
                VerificationRunRow,
                VerificationRunRow.operation_execution_id == OperationExecutionRow.id,
            )
            .where(IncidentRow.id == incident_id)
        )
        async with self._session_factory() as session:
            row = (await session.execute(statement)).one_or_none()
        if row is None:
            raise IncidentNotFound(str(incident_id))

        incident = cast(IncidentRow, row[0])
        run = cast(InvestigationRunRow | None, row[1])
        hypothesis = cast(HypothesisRow | None, row[2])
        plan = cast(ActionPlanRow | None, row[3])
        approval = cast(ApprovalRequestRow | None, row[4])
        execution = cast(OperationExecutionRow | None, row[5])
        verification = cast(VerificationRunRow | None, row[6])
        return WorkroomSnapshot(
            incident_id=incident.id,
            incident_version=incident.version,
            investigation=_investigation_summary(run),
            evidence_board=_evidence_board_summary(
                run,
                hypothesis,
                evidence_count=cast(int, row[7]),
                report_count=cast(int, row[8]),
            ),
            action_plan=_action_plan_summary(plan, step_count=cast(int, row[9])),
            approval_request=_approval_summary(approval),
            operation_execution=_execution_summary(execution),
            verification_run=_verification_summary(
                verification,
                sample_count=cast(int, row[10]),
            ),
        )


def _investigation_summary(
    row: InvestigationRunRow | None,
) -> WorkroomInvestigationSummary | None:
    if row is None:
        return None
    error = row.last_error or {}
    return WorkroomInvestigationSummary(
        id=row.id,
        mode=InvestigationMode(row.mode),
        data_source_mode=InvestigationDataSourceMode(row.data_source_mode),
        status=InvestigationRunStatus(row.status),
        prompt_version=row.prompt_version,
        model_name=row.model_name,
        error_code=_optional_string(error.get("error_code")),
        error_message=_optional_string(error.get("message")),
        created_at=row.created_at,
        started_at=row.started_at,
        completed_at=row.completed_at,
        updated_at=row.updated_at,
    )


def _evidence_board_summary(
    run: InvestigationRunRow | None,
    hypothesis: HypothesisRow | None,
    *,
    evidence_count: int,
    report_count: int,
) -> WorkroomEvidenceBoardSummary | None:
    if run is None:
        return None
    return WorkroomEvidenceBoardSummary(
        evidence_count=evidence_count,
        specialist_report_count=report_count,
        primary_hypothesis=(
            WorkroomHypothesisSummary(
                id=hypothesis.id,
                root_cause=RootCause(hypothesis.root_cause),
                statement=hypothesis.statement,
                confidence=hypothesis.confidence,
                status=HypothesisStatus(hypothesis.status),
                challenge_summary=hypothesis.challenge_summary,
                rule_version=hypothesis.rule_version,
            )
            if hypothesis is not None
            else None
        ),
    )


def _action_plan_summary(
    row: ActionPlanRow | None,
    *,
    step_count: int,
) -> WorkroomActionPlanSummary | None:
    if row is None:
        return None
    return WorkroomActionPlanSummary(
        id=row.id,
        version=row.version,
        status=ActionPlanStatus(row.status),
        summary=row.summary,
        risk_level=RiskLevel(row.risk_level),
        requires_human_approval=row.requires_human_approval,
        step_count=step_count,
        created_at=row.created_at,
    )


def _approval_summary(row: ApprovalRequestRow | None) -> WorkroomApprovalSummary | None:
    if row is None:
        return None
    return WorkroomApprovalSummary(
        id=row.id,
        status=ApprovalRequestStatus(row.status),
        requested_by_id=row.requested_by_id,
        request_reason=row.request_reason,
        expires_at=row.expires_at,
        created_at=row.created_at,
        decided_by_id=row.decided_by_id,
        decision_reason=row.decision_reason,
        decided_at=row.decided_at,
    )


def _execution_summary(row: OperationExecutionRow | None) -> WorkroomExecutionSummary | None:
    if row is None:
        return None
    return WorkroomExecutionSummary(
        id=row.id,
        action_step_id=row.action_step_id,
        status=ExecutionStatus(row.status),
        executor_name=row.executor_name,
        attempt_count=row.attempt_count,
        error_code=row.error_code,
        error_message=row.error_message,
        created_at=row.created_at,
        updated_at=row.updated_at,
        completed_at=row.completed_at,
    )


def _verification_summary(
    row: VerificationRunRow | None,
    *,
    sample_count: int,
) -> WorkroomVerificationSummary | None:
    if row is None:
        return None
    return WorkroomVerificationSummary(
        id=row.id,
        status=VerificationStatus(row.status),
        signal=row.signal,
        threshold=row.threshold,
        observation_window_seconds=row.observation_window_seconds,
        required_successful_samples=row.required_successful_samples,
        successful_sample_count=row.successful_sample_count,
        sample_count=sample_count,
        failure_code=row.failure_code,
        failure_message=row.failure_message,
        last_observed_at=row.last_observed_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
        completed_at=row.completed_at,
    )


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) else None
