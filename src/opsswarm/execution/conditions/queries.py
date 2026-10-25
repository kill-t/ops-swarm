"""完整核验版本前缀与可信命令的历史重建。"""

import hashlib
import json
from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from opsswarm.execution.conditions.authorization import validate_admission
from opsswarm.execution.conditions.contracts import (
    MAX_VERSIONS,
    POLICY_VERSION,
    ConditionAuditRecord,
    ConditionCommand,
    ConditionError,
    ConditionRecord,
)
from opsswarm.execution.conditions.policy import build_content, expires_at, validate_version
from opsswarm.execution.conditions.sources import ConditionSources
from opsswarm.execution.conditions.tables import CandidateConditionCommandRow, CandidateConditionRow
from opsswarm.identity.tables import OperatorAuthorizationRow
from opsswarm.investigation.context.hashing import content_hash


def verification_id(decision_id: UUID, version: int) -> UUID:
    return uuid5(decision_id, f"candidate-condition-verification:v1:{version}")


def matches_command(row: CandidateConditionRow, job_id: UUID, command: ConditionCommand) -> bool:
    return (
        row.candidate_job_id == job_id
        and row.decision_id == command.decision_id
        and row.decision_content_sha256 == command.decision_content_sha256
        and row.incident_version == command.expected_incident_version
        and row.version == command.expected_condition_version + 1
        and row.previous_content_sha256 == command.previous_content_sha256
        and row.idempotency_key == command.idempotency_key
        and row.command_sha256 == content_hash(command)
    )


async def validated_audit(
    session: AsyncSession, row: CandidateConditionCommandRow
) -> ConditionAuditRecord:
    try:
        record = ConditionAuditRecord.model_validate(row, from_attributes=True)
        validate_admission(
            await session.get(OperatorAuthorizationRow, row.authorization_id),
            run_id=row.investigation_run_id,
            job_id=row.candidate_job_id,
            subject_id=row.subject_id,
            command=record.command,
            raw_body=row.raw_body,
            occurred_at=row.created_at,
        )
        if row.command != record.command.model_dump(
            mode="json"
        ) or row.command_sha256 != content_hash(record.command):
            raise ConditionError("candidate_condition_integrity_violation", "核验命令或摘要不一致")
        if row.outcome != "error":
            fact = await session.get(CandidateConditionRow, row.verification_id)
            if (
                fact is None
                or fact.investigation_run_id != row.investigation_run_id
                or fact.verified_by_id != row.subject_id
                or not matches_command(fact, row.candidate_job_id, record.command)
                or row.created_at < fact.created_at
                or row.outcome == "applied"
                and (
                    fact.authorization_id != row.authorization_id
                    or fact.created_at != row.created_at
                )
            ):
                raise ConditionError(
                    "candidate_condition_integrity_violation", "核验审计缺少同源事实"
                )
        return record
    except ConditionError:
        raise
    except ValueError as error:
        raise ConditionError(
            "candidate_condition_integrity_violation", "核验审计无法完整重建"
        ) from error


async def read_chain(
    session: AsyncSession, sources: ConditionSources
) -> tuple[ConditionRecord, ...]:
    try:
        return await _read_chain(session, sources)
    except ConditionError:
        raise
    except ValueError as error:
        raise ConditionError(
            "candidate_condition_integrity_violation", "核验版本历史无法完整重建"
        ) from error


async def _read_chain(
    session: AsyncSession, sources: ConditionSources
) -> tuple[ConditionRecord, ...]:
    context = sources.context
    decision = context.decision.record
    assert decision is not None
    rows = list(
        await session.scalars(
            select(CandidateConditionRow)
            .where(
                CandidateConditionRow.decision_id == decision.id,
            )
            .order_by(CandidateConditionRow.version)
            .limit(MAX_VERSIONS + 1)
        )
    )
    if len(rows) > MAX_VERSIONS:
        raise ConditionError("candidate_condition_integrity_violation", "核验版本越过冻结上限")
    records: list[ConditionRecord] = []
    previous = None
    for version, row in enumerate(rows, start=1):
        # 原始JSON也必须匹配摘要，防止Python把数字0/1与布尔值视为相等。
        actual_hash = hashlib.sha256(
            json.dumps(
                row.content,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        if row.content_sha256 != actual_hash:
            raise ConditionError(
                "candidate_condition_integrity_violation", "核验原始内容摘要不一致"
            )
        record = ConditionRecord.model_validate(row, from_attributes=True)
        creator = await session.scalar(
            select(CandidateConditionCommandRow).where(
                CandidateConditionCommandRow.authorization_id == row.authorization_id
            )
        )
        if creator is None or creator.outcome != "applied" or creator.verification_id != row.id:
            raise ConditionError(
                "candidate_condition_integrity_violation", "核验版本缺少原始成功命令"
            )
        audit = await validated_audit(session, creator)
        validate_version(audit.command, previous)
        deadlines = await sources.validate(audit.command.items, at=row.created_at)
        if (
            row.version != version
            or row.id != verification_id(decision.id, version)
            or row.policy_version != POLICY_VERSION
            or row.investigation_run_id != decision.investigation_run_id
            or row.candidate_job_id != decision.candidate_job_id
            or row.decision_content_sha256 != decision.content_sha256
            or row.incident_version != decision.incident_version
            or row.previous_id != (previous.id if previous else None)
            or row.previous_content_sha256 != (previous.content_sha256 if previous else None)
            or not matches_command(row, decision.candidate_job_id, audit.command)
            or row.created_at < (previous.created_at if previous else decision.created_at)
            or row.created_at >= row.expires_at
            or row.expires_at
            != expires_at(
                row.created_at, context.request.expires_at, audit.command.items, deadlines
            )
            or record.content != build_content(decision, context.plan, audit.command)
            or row.content != record.content.model_dump(mode="json")
            or row.content_sha256 != content_hash(record.content)
        ):
            raise ConditionError(
                "candidate_condition_integrity_violation", "核验版本与目录、前驱或来源不一致"
            )
        records.append(record)
        previous = record
    return tuple(records)
