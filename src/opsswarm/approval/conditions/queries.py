"""新核验请求、决定及两阶段可信审计的完整历史重建。"""

import hashlib
import json
from dataclasses import dataclass
from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from opsswarm.approval.conditions.authorization import Command, validate_admission
from opsswarm.approval.conditions.contracts import (
    POLICY_VERSION,
    ApprovalAuditRecord,
    ApprovalError,
    DecisionCommand,
    DecisionRecord,
    RequestCommand,
    RequestRecord,
)
from opsswarm.approval.conditions.policy import (
    build_decision_content,
    build_request_content,
    validate_timing,
)
from opsswarm.approval.conditions.sources import ApprovalBasis
from opsswarm.approval.conditions.tables import (
    ConditionApprovalCommandRow,
    ConditionApprovalDecisionRow,
    ConditionApprovalRequestRow,
)
from opsswarm.identity.tables import OperatorAuthorizationRow
from opsswarm.investigation.context.hashing import content_hash


@dataclass(frozen=True)
class ApprovalFacts:
    request: RequestRecord | None
    decision: DecisionRecord | None


def request_id(verification_id: UUID) -> UUID:
    return uuid5(verification_id, "condition-approval-request:v1")


def decision_id(identifier: UUID) -> UUID:
    return uuid5(identifier, "condition-approval-decision:v1")


def matches_request(
    row: ConditionApprovalRequestRow, job_id: UUID, version: int, command: RequestCommand
) -> bool:
    return (
        row.candidate_job_id == job_id
        and row.verification_version == version
        and row.verification_id == command.verification_id
        and row.verification_content_sha256 == command.verification_content_sha256
        and row.incident_version == command.expected_incident_version
        and row.idempotency_key == command.idempotency_key
        and row.command_sha256 == content_hash(command)
    )


def matches_decision(
    row: ConditionApprovalDecisionRow, job_id: UUID, version: int, command: DecisionCommand
) -> bool:
    return (
        row.candidate_job_id == job_id
        and row.verification_version == version
        and row.request_id == command.request_id
        and row.request_content_sha256 == command.request_content_sha256
        and row.incident_version == command.expected_incident_version
        and row.idempotency_key == command.idempotency_key
        and row.command_sha256 == content_hash(command)
    )


def raw_content_hash(content: object) -> str:
    # 数字0/1与布尔值在Python中相等，必须对实际JSON单独取摘要。
    return hashlib.sha256(
        json.dumps(
            content, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    ).hexdigest()


async def validated_audit(
    session: AsyncSession, row: ConditionApprovalCommandRow
) -> ApprovalAuditRecord:
    try:
        record = ApprovalAuditRecord.model_validate(row, from_attributes=True)
        validate_admission(
            await session.get(OperatorAuthorizationRow, row.authorization_id),
            run_id=row.investigation_run_id,
            job_id=row.candidate_job_id,
            version=row.verification_version,
            subject_id=row.subject_id,
            command=record.command,
            raw_body=row.raw_body,
            occurred_at=row.created_at,
        )
        if row.command != record.command.model_dump(
            mode="json"
        ) or row.command_sha256 != content_hash(record.command):
            raise ApprovalError("condition_approval_integrity_violation", "审批命令或摘要不一致")
        if row.outcome != "error":
            command: Command = record.command
            fact: ConditionApprovalRequestRow | ConditionApprovalDecisionRow | None
            if isinstance(command, RequestCommand):
                request = await session.get(ConditionApprovalRequestRow, row.request_id)
                valid = (
                    request is not None
                    and row.decision_id is None
                    and matches_request(
                        request, row.candidate_job_id, row.verification_version, command
                    )
                )
                fact = request
                subject = request.requested_by_id if request else None
            else:
                decision = await session.get(ConditionApprovalDecisionRow, row.decision_id)
                valid = (
                    decision is not None
                    and row.request_id == decision.request_id
                    and matches_decision(
                        decision, row.candidate_job_id, row.verification_version, command
                    )
                )
                fact = decision
                subject = decision.decided_by_id if decision else None
            if (
                not valid
                or fact is None
                or fact.investigation_run_id != row.investigation_run_id
                or subject != row.subject_id
                or row.created_at < fact.created_at
                or row.outcome == "applied"
                and (
                    fact.authorization_id != row.authorization_id
                    or fact.created_at != row.created_at
                )
                or row.outcome == "replayed"
                and fact.authorization_id == row.authorization_id
            ):
                raise ApprovalError(
                    "condition_approval_integrity_violation", "审批审计缺少同源事实"
                )
        return record
    except ApprovalError:
        raise
    except ValueError as error:
        raise ApprovalError(
            "condition_approval_integrity_violation", "审批审计无法完整重建"
        ) from error


async def read_facts(session: AsyncSession, basis: ApprovalBasis) -> ApprovalFacts:
    try:
        return await _read_facts(session, basis)
    except ApprovalError:
        raise
    except ValueError as error:
        raise ApprovalError(
            "condition_approval_integrity_violation", "审批历史无法完整重建"
        ) from error


async def _read_facts(session: AsyncSession, basis: ApprovalBasis) -> ApprovalFacts:
    verification = basis.verification
    row = await session.scalar(
        select(ConditionApprovalRequestRow).where(
            ConditionApprovalRequestRow.verification_id == verification.id
        )
    )
    if row is None:
        if (
            await session.scalar(
                select(ConditionApprovalDecisionRow.id).where(
                    ConditionApprovalDecisionRow.verification_id == verification.id
                )
            )
            is not None
        ):
            raise ApprovalError("condition_approval_integrity_violation", "审批决定缺少原请求")
        return ApprovalFacts(None, None)
    record = RequestRecord.model_validate(row, from_attributes=True)
    creator = await session.scalar(
        select(ConditionApprovalCommandRow).where(
            ConditionApprovalCommandRow.authorization_id == row.authorization_id
        )
    )
    if (
        creator is None
        or creator.stage != "request"
        or creator.outcome != "applied"
        or creator.request_id != row.id
        or creator.decision_id is not None
    ):
        raise ApprovalError("condition_approval_integrity_violation", "请求缺少原始成功命令")
    audit = await validated_audit(session, creator)
    if (
        not isinstance(audit.command, RequestCommand)
        or row.id != request_id(verification.id)
        or row.policy_version != POLICY_VERSION
        or row.investigation_run_id != verification.investigation_run_id
        or row.candidate_job_id != verification.candidate_job_id
        or row.verification_version != verification.version
        or row.verification_content_sha256 != verification.content_sha256
        or row.incident_version != verification.incident_version
        or not verification.created_at <= row.created_at < row.expires_at
        or row.expires_at != verification.expires_at
        or record.content != build_request_content(verification)
        or row.content != record.content.model_dump(mode="json")
        or row.content_sha256 != content_hash(record.content)
        or row.content_sha256 != raw_content_hash(row.content)
    ):
        raise ApprovalError("condition_approval_integrity_violation", "请求与原核验或内容不一致")
    decision_row = await session.scalar(
        select(ConditionApprovalDecisionRow).where(
            ConditionApprovalDecisionRow.request_id == row.id
        )
    )
    if decision_row is None:
        return ApprovalFacts(record, None)
    decided = DecisionRecord.model_validate(decision_row, from_attributes=True)
    creator = await session.scalar(
        select(ConditionApprovalCommandRow).where(
            ConditionApprovalCommandRow.authorization_id == decision_row.authorization_id
        )
    )
    if (
        creator is None
        or creator.stage != "decision"
        or creator.outcome != "applied"
        or creator.request_id != row.id
        or creator.decision_id != decision_row.id
    ):
        raise ApprovalError("condition_approval_integrity_violation", "决定缺少原始成功命令")
    audit = await validated_audit(session, creator)
    if not isinstance(audit.command, DecisionCommand):
        raise ApprovalError("condition_approval_integrity_violation", "决定命令阶段错误")
    validate_timing(audit.command.action, record, decision_row.created_at)
    expected = build_decision_content(record, audit.command.action)
    if (
        decision_row.id != decision_id(row.id)
        or decision_row.policy_version != POLICY_VERSION
        or decision_row.investigation_run_id != row.investigation_run_id
        or decision_row.candidate_job_id != row.candidate_job_id
        or decision_row.request_content_sha256 != row.content_sha256
        or decision_row.verification_id != verification.id
        or decision_row.verification_version != verification.version
        or decision_row.verification_content_sha256 != verification.content_sha256
        or decision_row.incident_version != verification.incident_version
        or decision_row.outcome != expected.outcome
        or decided.content != expected
        or decision_row.content != decided.content.model_dump(mode="json")
        or decision_row.content_sha256 != content_hash(decided.content)
        or decision_row.content_sha256 != raw_content_hash(decision_row.content)
    ):
        raise ApprovalError("condition_approval_integrity_violation", "决定与原请求/核验不一致")
    return ApprovalFacts(record, decided)
