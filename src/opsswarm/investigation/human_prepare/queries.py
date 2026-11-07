"""历史准备来源、不可变工件与当前适用性的事务内重建。"""

from collections.abc import Callable
from datetime import datetime
from uuid import uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from opsswarm.identity.tables import OperatorAuthorizationRow
from opsswarm.investigation.budget.queries import read_budget_snapshot
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.human_prepare.authorization import validate_admission
from opsswarm.investigation.human_prepare.builder import (
    build_prepared_input,
    diagnose_preparation,
    make_artifact,
)
from opsswarm.investigation.human_prepare.contracts import (
    POLICY_VERSION,
    PreparationArtifact,
    PreparationAttemptView,
    PreparationAuditRecord,
    PreparationError,
    PreparationJobView,
    PreparationSource,
    PrepareCommand,
)
from opsswarm.investigation.human_prepare.source import load_preparation_source
from opsswarm.investigation.human_prepare.tables import (
    HumanPreparationArtifactRow,
    HumanPreparationAttemptRow,
    HumanPreparationCommandRow,
    HumanPreparationJobRow,
)
from opsswarm.investigation.human_wait.state import load_wait_state
from opsswarm.investigation.persistence.lease import normalized_now
from opsswarm.investigation.persistence.tables import InvestigationRunRow


def source_matches_command(job: HumanPreparationJobRow, command: PrepareCommand) -> bool:
    return (
        job.handoff_id == command.handoff_id
        and job.wait_version == command.expected_wait_version
        and job.source.get("handoff", {}).get("source_sha256") == command.source_sha256
        and job.idempotency_key == command.idempotency_key
        and job.command_sha256 == content_hash(command)
    )


async def validated_audit(
    session: AsyncSession, row: HumanPreparationCommandRow
) -> PreparationAuditRecord:
    try:
        record = PreparationAuditRecord.model_validate(row, from_attributes=True)
        command = record.command
        validate_admission(
            await session.get(OperatorAuthorizationRow, row.authorization_id),
            run_id=row.investigation_run_id,
            subject_id=row.subject_id,
            command=command,
            raw_body=row.raw_body,
            occurred_at=row.created_at,
        )
        if row.command != command.model_dump(mode="json") or row.command_sha256 != content_hash(
            command
        ):
            raise ValueError("准备规范命令不一致")
        if row.outcome != "error":
            job = await session.get(HumanPreparationJobRow, row.job_id)
            if (
                job is None
                or job.investigation_run_id != row.investigation_run_id
                or job.requested_by_id != row.subject_id
                or not source_matches_command(job, command)
                or row.created_at < job.created_at
                or (
                    row.outcome == "applied"
                    and (
                        job.authorization_id != row.authorization_id
                        or job.created_at != row.created_at
                    )
                )
            ):
                raise ValueError("准备结果缺少匹配任务")
        return record
    except ValueError as error:
        raise PreparationError(
            "human_preparation_integrity_violation", "准备业务结果来源不一致"
        ) from error


async def validated_source(
    session: AsyncSession, row: HumanPreparationJobRow, run: InvestigationRunRow
) -> PreparationSource:
    try:
        source = PreparationSource.model_validate(row.source)
        audit = await session.scalar(
            select(HumanPreparationCommandRow).where(
                HumanPreparationCommandRow.authorization_id == row.authorization_id
            )
        )
        if (
            row.policy_version != POLICY_VERSION
            or row.id != uuid5(run.id, f"human-prepare:{row.wait_version}")
            or row.investigation_run_id != run.id
            or row.handoff_id != source.handoff.id
            or row.wait_version != source.wait.state_version
            or content_hash(source) != row.source_sha256
            or source.model_dump(mode="json") != row.source
            or audit is None
            or audit.outcome != "applied"
            or audit.job_id != row.id
            or row.created_at < source.wait.events[-1].created_at
        ):
            raise ValueError("准备任务缺少精确来源")
        await validated_audit(session, audit)
        rebuilt = await load_preparation_source(session, run, wait_version=row.wait_version)
        if rebuilt != source:
            raise ValueError("准备来源发生漂移")
        return source
    except ValueError as error:
        raise PreparationError(
            "human_preparation_integrity_violation", "准备任务来源无法通过核验"
        ) from error


async def read_job(
    session: AsyncSession,
    row: HumanPreparationJobRow,
    run: InvestigationRunRow,
    *,
    clock: Callable[[], datetime],
) -> PreparationJobView:
    view, _ = await read_job_with_source(session, row, run, clock=clock)
    return view


async def read_job_with_source(
    session: AsyncSession,
    row: HumanPreparationJobRow,
    run: InvestigationRunRow,
    *,
    clock: Callable[[], datetime],
) -> tuple[PreparationJobView, PreparationSource]:
    source = await validated_source(session, row, run)
    attempts = list(
        await session.scalars(
            select(HumanPreparationAttemptRow)
            .where(HumanPreparationAttemptRow.job_id == row.id)
            .order_by(HumanPreparationAttemptRow.number)
        )
    )
    stored = await session.get(HumanPreparationArtifactRow, row.id)
    current_budget = await read_budget_snapshot(session, run)
    if len(attempts) != row.attempt_count or any(a.number != i + 1 for i, a in enumerate(attempts)):
        raise PreparationError("human_preparation_integrity_violation", "准备尝试序列不连续")
    artifact = None
    if stored is not None:
        try:
            artifact = PreparationArtifact.model_validate(stored.content)
            rebuilt = make_artifact(
                source, build_prepared_input(source), artifact.budget, now=stored.created_at
            )
            captured = artifact.budget.calls
            end = captured[-1].sequence if captured else 0
            if (
                source.review.source_call_id not in {call.id for call in captured}
                or captured != tuple(call for call in current_budget.calls if call.sequence <= end)
                or any(
                    call.sequence > end and call.reserved_at < stored.created_at
                    for call in current_budget.calls
                )
            ):
                raise ValueError("准备预算快照不匹配实际账本前缀")
            if (
                stored.version != artifact.version
                or stored.content_sha256 != content_hash(artifact)
                or stored.content != artifact.model_dump(mode="json")
                or rebuilt != artifact
                or row.status != "succeeded"
                or row.completed_at != stored.created_at
                or not attempts
                or attempts[-1].id != stored.attempt_id
                or attempts[-1].status != "succeeded"
                or attempts[-1].finished_at != stored.created_at
            ):
                raise ValueError("成功准备工件与任务或内容不一致")
        except ValueError as error:
            raise PreparationError(
                "human_preparation_integrity_violation", "准备工件来源无法通过核验"
            ) from error
    elif row.status == "succeeded":
        raise PreparationError("human_preparation_integrity_violation", "成功准备缺少工件")
    _, current = await load_wait_state(session, run.id)
    availability = "pending"
    reason = None
    if current.status != "waiting" or current.state_version != row.wait_version:
        availability = "stale"
        reason = (
            "human_wait_cancelled" if current.status != "waiting" else "human_wait_version_changed"
        )
    elif row.status == "failed":
        availability, reason = "failed", row.last_error_code
    elif artifact is not None:
        diagnostic = diagnose_preparation(
            source,
            artifact.prepared_input,
            current_budget,
            now=normalized_now(clock),
        )
        availability, reason = diagnostic.status, diagnostic.reason_code
    view = PreparationJobView.model_validate(
        {
            **{
                name: getattr(row, name)
                for name in (
                    "id",
                    "investigation_run_id",
                    "wait_version",
                    "policy_version",
                    "source_sha256",
                    "requested_by_id",
                    "authorization_id",
                    "status",
                    "created_at",
                    "completed_at",
                    "last_error_code",
                )
            },
            "attempts": tuple(
                PreparationAttemptView.model_validate(a, from_attributes=True) for a in attempts
            ),
            "artifact": artifact,
            "artifact_sha256": stored.content_sha256 if stored is not None else None,
            "availability": availability,
            "availability_reason": reason,
        }
    )
    return view, source
