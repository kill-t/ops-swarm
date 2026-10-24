"""从原请求、计划及可信命令重建独立决定与决定阶段适用性。"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from opsswarm.approval.candidate.contracts import RequestError, RequestView
from opsswarm.approval.candidate.queries import read_request
from opsswarm.approval.decision.authorization import validate_admission
from opsswarm.approval.decision.contracts import (
    POLICY_VERSION,
    DecisionAuditRecord,
    DecisionCommand,
    DecisionError,
    DecisionRecord,
    DecisionView,
)
from opsswarm.approval.decision.policy import build_content, decision_availability, target
from opsswarm.approval.decision.tables import CandidateDecisionCommandRow, CandidateDecisionRow
from opsswarm.identity.tables import OperatorAuthorizationRow
from opsswarm.incidents.persistence.tables import IncidentEventRow, IncidentRow
from opsswarm.investigation.candidate.contracts import CandidatePacket, CandidateSource
from opsswarm.investigation.candidate.tables import CandidateJobRow
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.human_wait.tables import HumanWaitEventRow
from opsswarm.investigation.persistence.lease import normalized_now
from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.planning.candidate.contracts import PlanRecord
from opsswarm.planning.candidate.tables import CandidatePlanRow


@dataclass(frozen=True)
class DecisionContext:
    request: RequestView
    plan: PlanRecord
    source: CandidateSource
    packet: CandidatePacket
    source_content_sha256: str


async def load_context(
    session: AsyncSession, run: InvestigationRunRow, job_id: UUID, *, clock: Callable[[], datetime]
) -> DecisionContext:
    try:
        request = await read_request(session, run, job_id, clock=clock)
        if request.record is None:
            raise DecisionError("candidate_decision_request_not_found", "候选尚无可信审批请求", 404)
        job = await session.get(CandidateJobRow, job_id)
        plan = await session.get(CandidatePlanRow, request.record.plan_id)
        assert job is not None and plan is not None
        # read_request 已核验同一不可变正文的完整链，不再递归遍历相同候选来源。
        return DecisionContext(
            request,
            PlanRecord.model_validate(plan, from_attributes=True),
            CandidateSource.model_validate(job.source),
            CandidatePacket.model_validate(job.packet),
            job.source_content_sha256,
        )
    except RequestError as error:
        raise DecisionError(
            "candidate_decision_source_invalid", "审批决定来源无法通过核验", error.status_code
        ) from error


def matches_command(row: CandidateDecisionRow, job_id: UUID, command: DecisionCommand) -> bool:
    return (
        row.candidate_job_id == job_id
        and row.request_id == command.request_id
        and row.plan_content_sha256 == command.plan_content_sha256
        and row.previous_incident_version == command.expected_incident_version
        and row.idempotency_key == command.idempotency_key
        and row.command_sha256 == content_hash(command)
    )


async def validated_audit(
    session: AsyncSession, row: CandidateDecisionCommandRow
) -> DecisionAuditRecord:
    record = DecisionAuditRecord.model_validate(row, from_attributes=True)
    validate_admission(
        await session.get(OperatorAuthorizationRow, row.authorization_id),
        run_id=row.investigation_run_id,
        job_id=row.candidate_job_id,
        subject_id=row.subject_id,
        command=record.command,
        raw_body=row.raw_body,
        occurred_at=row.created_at,
    )
    if row.command != record.command.model_dump(mode="json") or row.command_sha256 != content_hash(
        record.command
    ):
        raise DecisionError("candidate_decision_integrity_violation", "审批决定命令或摘要不一致")
    if row.outcome != "error":
        decision = await session.get(CandidateDecisionRow, row.decision_id)
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
            )
        ):
            raise DecisionError(
                "candidate_decision_integrity_violation", "决定审计缺少同源不可变事实"
            )
    return record


async def read_decision(
    session: AsyncSession, run: InvestigationRunRow, job_id: UUID, *, clock: Callable[[], datetime]
) -> DecisionView:
    try:
        context = await load_context(session, run, job_id, clock=clock)
        request = context.request.record
        assert request is not None
        row = await session.scalar(
            select(CandidateDecisionRow).where(CandidateDecisionRow.request_id == request.id)
        )
        if row is None:
            return DecisionView(record=None, availability="pending")
        record = DecisionRecord.model_validate(row, from_attributes=True)
        creator = await session.scalar(
            select(CandidateDecisionCommandRow).where(
                CandidateDecisionCommandRow.authorization_id == row.authorization_id
            )
        )
        if creator is None or creator.outcome != "applied" or creator.decision_id != row.id:
            raise DecisionError("candidate_decision_integrity_violation", "决定缺少原始成功命令")
        audit = await validated_audit(session, creator)
        outcome, action, status = target(audit.command.action)
        if (
            row.id != uuid5(request.id, "candidate-approval-decision:v1")
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
                source_content_sha256=context.source_content_sha256,
                action=audit.command.action,
            )
            or row.content != record.content.model_dump(mode="json")
            or row.content_sha256 != content_hash(record.content)
        ):
            raise DecisionError(
                "candidate_decision_integrity_violation", "审批决定无法重建原来源及精确内容"
            )
        event = await session.get(IncidentEventRow, row.event_cursor)
        if (
            event is None
            or event.incident_id != run.incident_id
            or event.incident_version != row.incident_version
            or event.event_type != "incident.status_changed"
            or event.from_status != "awaiting_approval"
            or event.to_status != status
            or event.actor_type != "human"
            or event.actor_id != row.decided_by_id
            or event.reason != audit.command.reason
            or event.occurred_at != row.created_at
            or event.payload != {"action": action.value}
        ):
            raise DecisionError(
                "candidate_decision_integrity_violation", "审批决定缺少精确事故迁移事件"
            )
        incident = await session.get(IncidentRow, run.incident_id)
        latest = await session.scalar(
            select(HumanWaitEventRow)
            .where(HumanWaitEventRow.investigation_run_id == run.id)
            .order_by(HumanWaitEventRow.state_version.desc())
            .limit(1)
        )
        return DecisionView(
            record=record,
            availability=decision_availability(
                record,
                source_kind=context.source.source_kind,
                run_status=run.status,
                latest_wait_version=latest.state_version if latest else None,
                wait_cancelled=latest is not None and latest.action == "cancel",
                incident_status=incident.status if incident else None,
                incident_version=incident.version if incident else None,
                expires_at=request.expires_at,
                now=normalized_now(clock),
            ),
        )
    except DecisionError:
        raise
    except ValueError as error:
        raise DecisionError(
            "candidate_decision_integrity_violation", "审批决定历史材料无法通过核验"
        ) from error
