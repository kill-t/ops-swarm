"""候选发布请求与完整审批来源的历史重建。"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from opsswarm.approval.conditions.contracts import DecisionRecord
from opsswarm.approval.conditions.queries import raw_content_hash, read_facts
from opsswarm.approval.conditions.sources import ApprovalBasis, load_basis, source_availability
from opsswarm.execution.release.authorization import validate_admission
from opsswarm.execution.release.contracts import (
    POLICY_VERSION,
    ReleaseRequestAuditRecord,
    ReleaseRequestCommand,
    ReleaseRequestError,
    ReleaseRequestRecord,
    ReleaseRequestView,
)
from opsswarm.execution.release.policy import build_content, expires_at
from opsswarm.execution.release.tables import CandidateReleaseCommandRow, CandidateReleaseRequestRow
from opsswarm.identity.tables import OperatorAuthorizationRow
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.persistence.lease import normalized_now
from opsswarm.investigation.persistence.tables import InvestigationRunRow


@dataclass(frozen=True)
class ReleaseContext:
    basis: ApprovalBasis
    approval: DecisionRecord


def request_id(identifier: UUID) -> UUID:
    return uuid5(identifier, "candidate-release-request:v1")


def operation_id(identifier: UUID) -> UUID:
    return uuid5(identifier, "candidate-release-operation:v1")


async def load_context(
    session: AsyncSession,
    run: InvestigationRunRow,
    job_id: UUID,
    version: int,
    *,
    clock: Callable[[], datetime],
) -> ReleaseContext:
    try:
        basis = await load_basis(session, run, job_id, version, clock=clock)
        facts = await read_facts(session, basis)
        if facts.decision is None:
            raise ReleaseRequestError(
                "candidate_release_approval_not_found", "核验审批决定不存在", 404
            )
        return ReleaseContext(basis, facts.decision)
    except ReleaseRequestError:
        raise
    except ValueError as error:
        raise ReleaseRequestError(
            "candidate_release_source_invalid",
            "发布请求审批来源无法完整重建",
            404 if getattr(error, "status_code", None) == 404 else 409,
        ) from error


def matches_command(
    row: CandidateReleaseRequestRow, job_id: UUID, version: int, command: ReleaseRequestCommand
) -> bool:
    return (
        row.candidate_job_id == job_id
        and row.verification_version == version
        and row.approval_decision_id == command.approval_decision_id
        and row.approval_content_sha256 == command.approval_content_sha256
        and row.incident_version == command.expected_incident_version
        and row.idempotency_key == command.idempotency_key
        and row.command_sha256 == content_hash(command)
    )


async def validated_audit(
    session: AsyncSession, row: CandidateReleaseCommandRow
) -> ReleaseRequestAuditRecord:
    try:
        audit = ReleaseRequestAuditRecord.model_validate(row, from_attributes=True)
        validate_admission(
            await session.get(OperatorAuthorizationRow, row.authorization_id),
            run_id=row.investigation_run_id,
            job_id=row.candidate_job_id,
            version=row.verification_version,
            subject_id=row.subject_id,
            command=audit.command,
            raw_body=row.raw_body,
            occurred_at=row.created_at,
        )
        if row.command != audit.command.model_dump(
            mode="json"
        ) or row.command_sha256 != content_hash(audit.command):
            raise ValueError("命令正文或摘要不一致")
        if row.outcome != "error":
            fact = await session.get(CandidateReleaseRequestRow, row.request_id)
            if (
                fact is None
                or fact.investigation_run_id != row.investigation_run_id
                or fact.requested_by_id != row.subject_id
                or not matches_command(
                    fact, row.candidate_job_id, row.verification_version, audit.command
                )
                or row.created_at < fact.created_at
                or row.outcome == "applied"
                and (
                    row.authorization_id != fact.authorization_id
                    or row.created_at != fact.created_at
                )
                or row.outcome == "replayed"
                and row.authorization_id == fact.authorization_id
            ):
                raise ValueError("业务审计缺少同源请求")
        return audit
    except ValueError as error:
        raise ReleaseRequestError(
            "candidate_release_integrity_violation", "发布请求审计无法完整重建"
        ) from error


async def read_record(
    session: AsyncSession, row: CandidateReleaseRequestRow, context: ReleaseContext
) -> ReleaseRequestRecord:
    try:
        record = ReleaseRequestRecord.model_validate(row, from_attributes=True)
        approval = context.approval
        creator = await session.scalar(
            select(CandidateReleaseCommandRow).where(
                CandidateReleaseCommandRow.authorization_id == row.authorization_id
            )
        )
        if creator is None or creator.outcome != "applied" or creator.request_id != row.id:
            raise ValueError("请求缺少原始成功命令")
        await validated_audit(session, creator)
        if (
            row.id != request_id(approval.id)
            or row.operation_id != operation_id(approval.id)
            or row.policy_version != POLICY_VERSION
            or row.investigation_run_id != approval.investigation_run_id
            or row.candidate_job_id != approval.candidate_job_id
            or row.approval_decision_id != approval.id
            or row.approval_content_sha256 != approval.content_sha256
            or row.incident_version != approval.incident_version
            or row.verification_version != approval.verification_version
            or row.created_at < approval.created_at
            or record.content.observation.observed_at < approval.created_at
            or row.expires_at
            != expires_at(
                context.basis.verification.expires_at, record.content.observation, row.created_at
            )
            or record.content
            != build_content(
                approval, context.basis.original.plan, record.content.observation, row.operation_id
            )
            or row.content != record.content.model_dump(mode="json")
            or row.content_sha256 != content_hash(record.content)
            or row.content_sha256 != raw_content_hash(row.content)
        ):
            raise ValueError("请求无法重建原批准与精确CAS")
        return record
    except ValueError as error:
        raise ReleaseRequestError(
            "candidate_release_integrity_violation", "发布请求历史无法完整重建"
        ) from error


async def view_for(
    session: AsyncSession,
    run: InvestigationRunRow,
    context: ReleaseContext,
    record: ReleaseRequestRecord | None,
    *,
    clock: Callable[[], datetime],
) -> ReleaseRequestView:
    if record is None:
        return ReleaseRequestView(record=None, availability="pending")
    availability = await source_availability(session, run, context.basis, clock=clock)
    if availability == "recorded":
        raise ReleaseRequestError("candidate_release_source_invalid", "核验来源适用性无效")
    if availability != "superseded" and normalized_now(clock) >= record.expires_at:
        availability = "expired"
    return ReleaseRequestView(record=record, availability=availability)
