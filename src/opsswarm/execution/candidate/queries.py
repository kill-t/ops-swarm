"""从批准历史重建检查事实，不把历史观测刷新成当前授权。"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID, uuid5

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from opsswarm.approval.candidate.contracts import RequestRecord
from opsswarm.approval.candidate.tables import CandidateApprovalRequestRow
from opsswarm.approval.decision.contracts import DecisionView
from opsswarm.approval.decision.queries import read_decision
from opsswarm.execution.candidate.authorization import validate_admission
from opsswarm.execution.candidate.contracts import (
    POLICY_VERSION,
    PreflightAuditRecord,
    PreflightCommand,
    PreflightError,
    PreflightRecord,
)
from opsswarm.execution.candidate.policy import build_content
from opsswarm.execution.candidate.tables import CandidatePreflightCommandRow, CandidatePreflightRow
from opsswarm.identity.tables import OperatorAuthorizationRow
from opsswarm.investigation.budget.policy import CallBudgetExceeded
from opsswarm.investigation.candidate.contracts import CandidatePacket, CandidateSource
from opsswarm.investigation.candidate.tables import CandidateJobRow
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.investigation.tasks.errors import TaskFactConflict
from opsswarm.planning.candidate.contracts import PlanRecord
from opsswarm.planning.candidate.tables import CandidatePlanRow


@dataclass(frozen=True)
class PreflightContext:
    decision: DecisionView
    request: RequestRecord
    plan: PlanRecord
    source: CandidateSource
    packet: CandidatePacket


async def load_context(
    session: AsyncSession, run: InvestigationRunRow, job_id: UUID, *, clock: Callable[[], datetime]
) -> PreflightContext:
    try:
        return await _load_context(session, run, job_id, clock=clock)
    except PreflightError:
        raise
    except (ValueError, CallBudgetExceeded, TaskFactConflict) as error:
        raise PreflightError(
            "candidate_preflight_source_invalid",
            "检查来源无法完整重建",
            404 if getattr(error, "status_code", None) == 404 else 409,
        ) from error


async def _load_context(
    session: AsyncSession, run: InvestigationRunRow, job_id: UUID, *, clock: Callable[[], datetime]
) -> PreflightContext:
    view = await read_decision(session, run, job_id, clock=clock)
    if view.record is None:
        raise PreflightError("candidate_preflight_decision_not_found", "候选尚无可信决定", 404)
    request = await session.get(CandidateApprovalRequestRow, view.record.request_id)
    plan = await session.get(CandidatePlanRow, view.record.content.plan_id)
    job = await session.get(CandidateJobRow, job_id)
    assert request is not None and plan is not None and job is not None
    # 决定读取已经核验同一不可变来源；解码这些行不重复递归核验整条链。
    return PreflightContext(
        view,
        RequestRecord.model_validate(request, from_attributes=True),
        PlanRecord.model_validate(plan, from_attributes=True),
        CandidateSource.model_validate(job.source),
        CandidatePacket.model_validate(job.packet),
    )


class _CheckIdentity(BaseModel):
    subject_id: str
    idempotency_key: str


def record_id(operation_id: UUID, subject_id: str, key: str) -> UUID:
    return uuid5(
        operation_id, content_hash(_CheckIdentity(subject_id=subject_id, idempotency_key=key))
    )


def matches_command(row: CandidatePreflightRow, job_id: UUID, command: PreflightCommand) -> bool:
    return (
        row.candidate_job_id == job_id
        and row.decision_id == command.decision_id
        and row.decision_content_sha256 == command.decision_content_sha256
        and row.incident_version == command.expected_incident_version
        and row.idempotency_key == command.idempotency_key
        and row.command_sha256 == content_hash(command)
    )


async def validated_audit(
    session: AsyncSession, row: CandidatePreflightCommandRow
) -> PreflightAuditRecord:
    try:
        return await _validated_audit(session, row)
    except PreflightError:
        raise
    except ValueError as error:
        raise PreflightError(
            "candidate_preflight_integrity_violation", "检查审计无法完整重建"
        ) from error


async def _validated_audit(
    session: AsyncSession, row: CandidatePreflightCommandRow
) -> PreflightAuditRecord:
    record = PreflightAuditRecord.model_validate(row, from_attributes=True)
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
        raise PreflightError("candidate_preflight_integrity_violation", "检查命令或摘要不一致")
    if row.outcome != "error":
        fact = await session.get(CandidatePreflightRow, row.preflight_id)
        if (
            fact is None
            or fact.investigation_run_id != row.investigation_run_id
            or fact.requested_by_id != row.subject_id
            or not matches_command(fact, row.candidate_job_id, record.command)
            or row.created_at < fact.created_at
            or row.outcome == "applied"
            and (fact.authorization_id != row.authorization_id or fact.created_at != row.created_at)
        ):
            raise PreflightError("candidate_preflight_integrity_violation", "检查审计缺少同源事实")
    return record


async def read_record(
    session: AsyncSession, row: CandidatePreflightRow, context: PreflightContext
) -> PreflightRecord:
    try:
        return await _read_record(session, row, context)
    except PreflightError:
        raise
    except ValueError as error:
        raise PreflightError(
            "candidate_preflight_integrity_violation", "检查历史无法完整重建"
        ) from error


async def _read_record(
    session: AsyncSession, row: CandidatePreflightRow, context: PreflightContext
) -> PreflightRecord:
    record = PreflightRecord.model_validate(row, from_attributes=True)
    decision = context.decision.record
    assert decision is not None
    creator = await session.scalar(
        select(CandidatePreflightCommandRow).where(
            CandidatePreflightCommandRow.authorization_id == row.authorization_id
        )
    )
    if creator is None or creator.outcome != "applied" or creator.preflight_id != row.id:
        raise PreflightError("candidate_preflight_integrity_violation", "检查缺少原始成功命令")
    audit = await validated_audit(session, creator)
    expected_operation = uuid5(decision.id, "candidate-operation:v1")
    if (
        decision.outcome != "approved"
        or row.policy_version != POLICY_VERSION
        or row.investigation_run_id != decision.investigation_run_id
        or row.candidate_job_id != decision.candidate_job_id
        or row.decision_id != decision.id
        or row.decision_content_sha256 != decision.content_sha256
        or row.incident_version != decision.incident_version
        or row.operation_id != expected_operation
        or row.id != record_id(expected_operation, row.requested_by_id, row.idempotency_key)
        or row.created_at < decision.created_at
        or not decision.created_at <= record.content.observation.observed_at <= row.created_at
        or row.expires_at
        != min(
            context.request.expires_at,
            record.content.observation.observed_at + timedelta(seconds=30),
        )
        or row.created_at >= row.expires_at
        or record.content
        != build_content(decision, context.plan.content.plan.steps[0], record.content.observation)
        or row.content != record.content.model_dump(mode="json")
        or row.content_sha256 != content_hash(record.content)
        or not matches_command(row, decision.candidate_job_id, audit.command)
    ):
        raise PreflightError("candidate_preflight_integrity_violation", "检查无法重建原批准与观测")
    return record
