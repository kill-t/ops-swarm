"""按不可变报告的精确身份加载并冻结经验来源。"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from opsswarm.approval.persistence.tables import ApprovalRequestRow
from opsswarm.execution.persistence.repository import stored_operation_execution_from_row
from opsswarm.execution.persistence.tables import OperationExecutionRow
from opsswarm.experience.contracts import ExperienceError, ExperienceSource, SourceEvidence
from opsswarm.incidents import IncidentStatus
from opsswarm.incidents.persistence.tables import IncidentRow
from opsswarm.investigation.models import (
    EvidenceKind,
    EvidenceObservation,
    EvidenceRelation,
    HypothesisStatus,
    InvestigationDataSourceMode,
    InvestigationMode,
    InvestigationRunStatus,
    RootCause,
)
from opsswarm.investigation.persistence.records import StoredHypothesis, StoredHypothesisEvidence
from opsswarm.investigation.persistence.tables import (
    EvidenceItemRow,
    HypothesisEvidenceRow,
    HypothesisRow,
    InvestigationRunRow,
    SpecialistReportEvidenceRow,
    SpecialistReportRow,
)
from opsswarm.planning import VerificationOperator, VerificationRule
from opsswarm.planning.persistence.repository import load_action_plan_in_session
from opsswarm.planning.persistence.tables import ActionPlanRow
from opsswarm.reports.errors import IncidentReportIntegrityViolation, IncidentReportNotFound
from opsswarm.reports.persistence.repository import load_incident_report_in_session
from opsswarm.verification.models import (
    VerificationFailure,
    VerificationSampleStatus,
    VerificationStatus,
)
from opsswarm.verification.persistence.records import (
    StoredVerificationRun,
    StoredVerificationSample,
)
from opsswarm.verification.persistence.tables import VerificationRunRow, VerificationSampleRow
from opsswarm.workroom.models import WorkroomInvestigationSummary


async def load_source(session: AsyncSession, report_id: UUID) -> ExperienceSource:
    """在任务创建事务中加载来源，任何缺失、漂移或越界均失败关闭。"""
    try:
        return await _load_source(session, report_id)
    except ExperienceError:
        raise
    except IncidentReportNotFound:
        raise ExperienceError("experience_report_not_found", "复盘报告不存在", 404) from None
    except (IncidentReportIntegrityViolation, ValueError, KeyError, RuntimeError):
        raise ExperienceError("source_conflict", "经验来源完整性或关联校验失败") from None


def _require(value: object) -> None:
    if value is None:
        raise ExperienceError("source_not_ready", "复盘尚无完整的调查与恢复验证来源")


async def _load_source(session: AsyncSession, report_id: UUID) -> ExperienceSource:
    report = await load_incident_report_in_session(session, report_id)
    incident = await session.get(IncidentRow, report.incident_id)
    _require(incident)
    if incident is None:
        raise AssertionError("前置检查已经拒绝缺失事故")
    if (
        incident.status != IncidentStatus.CLOSED.value
        or incident.version != report.incident_version
        or incident.service_name != report.content.incident.service_name
    ):
        raise ExperienceError("source_conflict", "复盘与事故最终状态不一致")
    room = report.content.workroom
    for stage in (
        room.investigation,
        room.evidence_board,
        room.action_plan,
        room.approval_request,
        room.operation_execution,
        room.verification_run,
    ):
        _require(stage)
    if (
        room.investigation is None
        or room.evidence_board is None
        or room.evidence_board.primary_hypothesis is None
        or room.action_plan is None
        or room.approval_request is None
        or room.operation_execution is None
        or room.verification_run is None
    ):
        raise ExperienceError("source_not_ready", "复盘尚无完整的调查与恢复验证来源")
    run = await session.get(InvestigationRunRow, room.investigation.id)
    hypothesis = await session.get(HypothesisRow, room.evidence_board.primary_hypothesis.id)
    plan_row = await session.get(ActionPlanRow, room.action_plan.id)
    execution_row = await session.get(OperationExecutionRow, room.operation_execution.id)
    verification_row = await session.get(VerificationRunRow, room.verification_run.id)
    approval_row = await session.get(ApprovalRequestRow, room.approval_request.id)
    if any(
        item is None
        for item in (run, hypothesis, plan_row, execution_row, verification_row, approval_row)
    ):
        raise ExperienceError("source_conflict", "复盘引用的持久来源已缺失")
    assert run is not None and hypothesis is not None and plan_row is not None
    assert execution_row is not None and verification_row is not None and approval_row is not None
    if run.incident_id != report.incident_id:
        raise ExperienceError("source_conflict", "复盘调查不属于当前事故")
    evidence_rows = tuple(
        await session.scalars(
            select(EvidenceItemRow)
            .where(EvidenceItemRow.investigation_run_id == run.id)
            .order_by(EvidenceItemRow.evidence_key)
            .limit(21)
        )
    )
    sample_rows = tuple(
        await session.scalars(
            select(VerificationSampleRow)
            .where(VerificationSampleRow.verification_run_id == verification_row.id)
            .order_by(VerificationSampleRow.sequence)
            .limit(101)
        )
    )
    if len(evidence_rows) > 20 or len(sample_rows) > 100:
        raise ExperienceError("source_too_large", "经验来源证据或验证样本超过上限")
    evidence_by_id = {item.id: item for item in evidence_rows}
    specialist_rows = tuple(
        await session.scalars(
            select(SpecialistReportRow)
            .where(SpecialistReportRow.investigation_run_id == run.id)
            .limit(11)
        )
    )
    if len(specialist_rows) != room.evidence_board.specialist_report_count:
        raise ExperienceError("source_conflict", "复盘专业报告数量发生漂移")
    specialist_ids = {item.id for item in specialist_rows}
    links = tuple(
        await session.scalars(
            select(SpecialistReportEvidenceRow)
            .where(SpecialistReportEvidenceRow.investigation_run_id == run.id)
            .limit(201)
        )
    )
    if len(links) > 200 or any(
        item.evidence_item_id not in evidence_by_id
        or item.specialist_report_id not in specialist_ids
        for item in links
    ):
        raise ExperienceError("source_conflict", "复盘专业报告证据引用没有闭包")
    relation_rows = tuple(
        await session.scalars(
            select(HypothesisEvidenceRow)
            .where(HypothesisEvidenceRow.hypothesis_id == hypothesis.id)
            .order_by(HypothesisEvidenceRow.evidence_item_id)
            .limit(21)
        )
    )
    if len(relation_rows) > 20 or any(
        item.investigation_run_id != run.id or item.evidence_item_id not in evidence_by_id
        for item in relation_rows
    ):
        raise ExperienceError("source_conflict", "复盘主假设证据引用没有闭包")
    plan = await load_action_plan_in_session(session, plan_row)
    execution = stored_operation_execution_from_row(execution_row)
    if (
        approval_row.action_plan_id != plan.id
        or approval_row.action_plan_version != plan.version
        or any(
            getattr(approval_row, name) != getattr(room.approval_request, name)
            for name in type(room.approval_request).model_fields
        )
    ):
        raise ExperienceError("source_conflict", "复盘历史审批关联发生漂移")
    _require_matching_execution(execution_row, room.operation_execution)
    verification = StoredVerificationRun(
        id=verification_row.id,
        operation_execution_id=verification_row.operation_execution_id,
        incident_id=verification_row.incident_id,
        action_step_id=verification_row.action_step_id,
        status=VerificationStatus(verification_row.status),
        verifier_name=verification_row.verifier_name,
        incident_version_before=verification_row.incident_version_before,
        rule=VerificationRule(
            signal=verification_row.signal,
            operator=VerificationOperator(verification_row.operator),
            threshold=verification_row.threshold,
            observation_window_seconds=verification_row.observation_window_seconds,
            required_successful_samples=verification_row.required_successful_samples,
        ),
        successful_sample_count=verification_row.successful_sample_count,
        first_success_at=verification_row.first_success_at,
        last_observed_at=verification_row.last_observed_at,
        failure=_failure(verification_row.failure_code, verification_row.failure_message),
        created_at=verification_row.created_at,
        updated_at=verification_row.updated_at,
        completed_at=verification_row.completed_at,
        samples=tuple(
            StoredVerificationSample(
                id=item.id,
                verification_run_id=item.verification_run_id,
                idempotency_key=item.idempotency_key,
                sequence=item.sequence,
                status=VerificationSampleStatus(item.status),
                signal=item.signal,
                observed_value=item.observed_value,
                threshold=item.threshold,
                http_status=item.http_status,
                failure=_failure(item.error_code, item.error_message, item.http_status),
                observed_at=item.observed_at,
            )
            for item in sample_rows
        ),
    )
    if (
        verification.created_at != room.verification_run.created_at
        or verification.updated_at != room.verification_run.updated_at
        or verification_row.failure_code != room.verification_run.failure_code
        or verification_row.failure_message != room.verification_run.failure_message
    ):
        raise ExperienceError("source_conflict", "复盘验证摘要发生漂移")
    error = run.last_error or {}
    return ExperienceSource(
        report_id=report.id,
        report_content_hash=report.content_hash,
        incident_id=report.incident_id,
        service_name=report.content.incident.service_name,
        data_source_mode=InvestigationDataSourceMode(run.data_source_mode),
        report=report,
        investigation=WorkroomInvestigationSummary(
            id=run.id,
            mode=InvestigationMode(run.mode),
            data_source_mode=InvestigationDataSourceMode(run.data_source_mode),
            status=InvestigationRunStatus(run.status),
            prompt_version=run.prompt_version,
            model_name=run.model_name,
            error_code=error.get("error_code"),
            error_message=error.get("message"),
            created_at=run.created_at,
            started_at=run.started_at,
            completed_at=run.completed_at,
            updated_at=run.updated_at,
        ),
        hypothesis=StoredHypothesis(
            id=hypothesis.id,
            investigation_run_id=hypothesis.investigation_run_id,
            root_cause=RootCause(hypothesis.root_cause),
            statement=hypothesis.statement,
            confidence=hypothesis.confidence,
            status=HypothesisStatus(hypothesis.status),
            challenge_summary=hypothesis.challenge_summary,
            rule_version=hypothesis.rule_version,
            evidence_assessments=tuple(
                StoredHypothesisEvidence(
                    evidence_item_id=item.evidence_item_id,
                    evidence_id=evidence_by_id[item.evidence_item_id].evidence_key,
                    relation=EvidenceRelation(item.relation),
                    rule_code=item.rule_code,
                    rationale=item.rationale,
                    created_at=item.created_at,
                )
                for item in relation_rows
            ),
            created_at=hypothesis.created_at,
        ),
        evidence=tuple(
            SourceEvidence(
                id=item.id,
                investigation_run_id=item.investigation_run_id,
                observation=EvidenceObservation(
                    evidence_id=item.evidence_key,
                    kind=EvidenceKind(item.kind),
                    source=item.source,
                    summary=item.summary,
                    observed_at=item.observed_at,
                    attributes=item.attributes,
                ),
                content_hash=item.content_hash,
            )
            for item in evidence_rows
        ),
        plan=plan,
        approval_plan_content_hash=approval_row.plan_content_hash,
        execution=execution,
        verification=verification,
    )


def _failure(
    code: str | None, message: str | None, http_status: int | None = None
) -> VerificationFailure | None:
    if code is None and message is None:
        return None
    if code is None or message is None:
        raise ValueError("验证失败信息不完整")
    return VerificationFailure(error_code=code, message=message, http_status=http_status)


def _require_matching_execution(row: OperationExecutionRow, summary: object) -> None:
    from opsswarm.workroom.models import WorkroomExecutionSummary

    if not isinstance(summary, WorkroomExecutionSummary) or any(
        getattr(row, name) != getattr(summary, name)
        for name in WorkroomExecutionSummary.model_fields
    ):
        raise ExperienceError("source_conflict", "复盘执行摘要发生漂移")
