"""从原请求、计划及可信命令重建独立决定与决定阶段适用性。"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid5

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from opsswarm.approval.reinvestigation_candidate.contracts import (
    ReflowRequestError,
    ReflowRequestView,
)
from opsswarm.approval.reinvestigation_candidate.queries import read_request
from opsswarm.approval.reinvestigation_decision.authorization import validate_admission
from opsswarm.approval.reinvestigation_decision.contracts import (
    POLICY_VERSION,
    ReflowDecisionAuditRecord,
    ReflowDecisionCommand,
    ReflowDecisionError,
    ReflowDecisionRecord,
    ReflowDecisionView,
)
from opsswarm.approval.reinvestigation_decision.policy import (
    build_content,
    decision_availability,
    target,
)
from opsswarm.approval.reinvestigation_decision.tables import (
    ReflowApprovalDecisionCommandRow,
    ReflowApprovalDecisionRow,
)
from opsswarm.identity.tables import OperatorAuthorizationRow
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.persistence.lease import normalized_now
from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.investigation.reinvestigation_candidate.contracts import (
    ReflowCandidatePacket,
    ReflowCandidateSource,
)
from opsswarm.investigation.reinvestigation_candidate.tables import ReflowCandidateJobRow
from opsswarm.planning.reinvestigation_candidate.contracts import ReflowPlanRecord
from opsswarm.planning.reinvestigation_candidate.tables import ReflowPlanRow


@dataclass(frozen=True)
class ReflowDecisionContext:
    request: ReflowRequestView
    plan: ReflowPlanRecord
    source: ReflowCandidateSource
    packet: ReflowCandidatePacket


async def load_context(
    session: AsyncSession, run: InvestigationRunRow, job_id: UUID, *, clock: Callable[[], datetime]
) -> ReflowDecisionContext:
    try:
        request = await read_request(session, run, job_id, clock=clock)
        if request.record is None:
            raise ReflowDecisionError(
                "reflow_decision_request_not_found", "候选尚无可信审批请求", 404
            )
        job = await session.get(ReflowCandidateJobRow, job_id)
        plan = await session.get(ReflowPlanRow, request.record.plan_id)
        assert job is not None and plan is not None
        # read_request 已核验同一不可变正文的完整链，不再递归遍历相同候选来源。
        return ReflowDecisionContext(
            request,
            ReflowPlanRecord.model_validate(plan, from_attributes=True),
            ReflowCandidateSource.model_validate(job.source),
            ReflowCandidatePacket.model_validate(job.packet),
        )
    except ReflowRequestError as error:
        raise ReflowDecisionError(
            "reflow_decision_source_invalid", "审批决定来源无法通过核验", error.status_code
        ) from error


def matches_command(
    row: ReflowApprovalDecisionRow, job_id: UUID, command: ReflowDecisionCommand
) -> bool:
    return (
        row.candidate_job_id == job_id
        and row.request_id == command.request_id
        and row.plan_content_sha256 == command.plan_content_sha256
        and row.previous_incident_version == command.expected_incident_version
        and row.idempotency_key == command.idempotency_key
        and row.command_sha256 == content_hash(command)
    )


async def validated_audit(
    session: AsyncSession, row: ReflowApprovalDecisionCommandRow
) -> ReflowDecisionAuditRecord:
    record = ReflowDecisionAuditRecord.model_validate(row, from_attributes=True)
    validate_admission(
        await session.get(OperatorAuthorizationRow, row.authorization_id),
        run_id=row.investigation_run_id,
        job_id=row.candidate_job_id,
        subject_id=row.subject_id,
        command=record.command,
        raw_body=row.raw_body,
        occurred_at=row.created_at,
    )
    if row.created_at >= row.credential_expires_at:
        raise ReflowDecisionError("reflow_decision_integrity_violation", "命令发生在凭据期限外")
    if row.command != record.command.model_dump(mode="json") or row.command_sha256 != content_hash(
        record.command
    ):
        raise ReflowDecisionError("reflow_decision_integrity_violation", "审批决定命令或摘要不一致")
    if row.outcome != "error":
        decision = await session.get(ReflowApprovalDecisionRow, row.decision_id)
        if (
            decision is None
            or decision.investigation_run_id != row.investigation_run_id
            or decision.decided_by_id != row.subject_id
            or not matches_command(decision, row.candidate_job_id, record.command)
            or decision.outcome != target(record.command.action)[0]
            or row.created_at < decision.created_at
            or row.outcome == "applied"
            and (
                decision.authorization_id != row.authorization_id
                or decision.created_at != row.created_at
                or decision.credential_expires_at != row.credential_expires_at
            )
        ):
            raise ReflowDecisionError(
                "reflow_decision_integrity_violation", "决定审计缺少同源不可变事实"
            )
    return record


async def read_decision(
    session: AsyncSession, run: InvestigationRunRow, job_id: UUID, *, clock: Callable[[], datetime]
) -> ReflowDecisionView:
    try:
        context = await load_context(session, run, job_id, clock=clock)
        request = context.request.record
        assert request is not None
        row = await session.scalar(
            select(ReflowApprovalDecisionRow).where(
                ReflowApprovalDecisionRow.request_id == request.id
            )
        )
        if row is None:
            return ReflowDecisionView(record=None, availability="pending")
        record = ReflowDecisionRecord.model_validate(row, from_attributes=True)
        creator = await session.scalar(
            select(ReflowApprovalDecisionCommandRow).where(
                ReflowApprovalDecisionCommandRow.authorization_id == row.authorization_id
            )
        )
        if creator is None or creator.outcome != "applied" or creator.decision_id != row.id:
            raise ReflowDecisionError("reflow_decision_integrity_violation", "决定缺少原始成功命令")
        audit = await validated_audit(session, creator)
        outcome, _, _ = target(audit.command.action)
        if (
            row.id != uuid5(request.id, "reinvestigation-approval-decision:v1")
            or row.policy_version != POLICY_VERSION
            or row.investigation_run_id != run.id
            or row.candidate_job_id != job_id
            or row.plan_content_sha256 != request.plan_content_sha256
            or row.previous_incident_version != request.incident_version
            or row.incident_version != request.incident_version + 1
            or row.created_at < request.created_at
            or row.outcome != outcome
            or (row.created_at >= request.expires_at) != (outcome == "expired")
            or record.content
            != build_content(
                request,
                context.plan,
                action=audit.command.action,
            )
            or row.content != record.content.model_dump(mode="json")
            or row.content_sha256 != content_hash(record.content)
        ):
            raise ReflowDecisionError(
                "reflow_decision_integrity_violation", "审批决定无法重建原来源及精确内容"
            )
        await session.execute(
            text(
                "SELECT assert_reflow_approval_decision(d,false) "
                "FROM reinvestigation_approval_decisions d WHERE id=:id"
            ),
            {"id": row.id},
        )
        blocker = await session.scalar(
            text(
                "SELECT reflow_approval_decision_basis_blocker(d,:at) "
                "FROM reinvestigation_approval_decisions d WHERE id=:id"
            ),
            {"id": row.id, "at": normalized_now(clock)},
        )
        if blocker not in (
            None,
            "reflow_source_changed",
            "run_deadline_exceeded",
            "reflow_approval_expired",
        ):
            raise ReflowDecisionError("reflow_decision_integrity_violation", "未知决定阶段阻断")
        return ReflowDecisionView(
            record=record,
            availability=decision_availability(
                record,
                basis_current=blocker is None,
                expires_at=request.expires_at,
                now=normalized_now(clock),
            ),
        )
    except ReflowDecisionError:
        raise
    except ValueError as error:
        raise ReflowDecisionError(
            "reflow_decision_integrity_violation", "审批决定历史材料无法通过核验"
        ) from error
