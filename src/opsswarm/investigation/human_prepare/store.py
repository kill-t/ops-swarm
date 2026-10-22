"""可信准备命令的精确幂等、事务审计与历史读取。"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.identity.models import OperatorAccessError, OperatorPrincipal, require_permission
from opsswarm.identity.tables import OperatorAuthorizationRow
from opsswarm.incidents.persistence.tables import IncidentRow
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.human_prepare.authorization import (
    OPERATION,
    check_principal,
    validate_admission,
)
from opsswarm.investigation.human_prepare.contracts import (
    POLICY_VERSION,
    PreparationAuditPage,
    PreparationError,
    PreparationJobView,
    PreparationResult,
    PrepareCommand,
)
from opsswarm.investigation.human_prepare.queries import (
    read_job,
    source_matches_command,
    validated_audit,
)
from opsswarm.investigation.human_prepare.source import load_preparation_source
from opsswarm.investigation.human_prepare.tables import (
    HumanPreparationCommandRow,
    HumanPreparationJobRow,
)
from opsswarm.investigation.human_wait.contracts import HumanWaitError
from opsswarm.investigation.human_wait.state import load_wait_state, lock_wait_run
from opsswarm.investigation.persistence.lease import normalized_now
from opsswarm.investigation.tasks.errors import TaskFactConflict


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class PreparationSubmission:
    result: PreparationResult | None = None
    error: PreparationError | None = None


class HumanPreparationStore:
    def __init__(
        self, factory: async_sessionmaker[AsyncSession], *, clock: Callable[[], datetime] = utc_now
    ) -> None:
        self._factory, self._clock = factory, clock

    def _check(self, principal: OperatorPrincipal) -> datetime:
        now = normalized_now(self._clock)
        check_principal(principal, now)
        return now

    async def create(
        self,
        run_id: UUID,
        command: PrepareCommand,
        *,
        principal: OperatorPrincipal,
        authorization_id: UUID,
        raw_body: bytes,
    ) -> PreparationSubmission:
        require_permission(principal, "POST", OPERATION)
        self._check(principal)
        if len(raw_body) > 65536:
            raise OperatorAccessError("operator_command_too_large", "准备命令超过 64 KiB", 413)
        try:
            raw = raw_body.decode("utf-8")
        except UnicodeError:
            raise OperatorAccessError(
                "operator_command_invalid", "准备命令必须为 UTF-8", 422
            ) from None
        async with self._factory() as session, session.begin():
            admission = await session.get(
                OperatorAuthorizationRow, authorization_id, with_for_update=True
            )
            now = self._check(principal)
            try:
                validate_admission(
                    admission,
                    run_id=run_id,
                    subject_id=principal.subject_id,
                    command=command,
                    raw_body=raw,
                    occurred_at=now,
                )
                if admission is None or admission.credential_id != principal.credential_id:
                    raise ValueError("凭据不一致")
            except ValueError:
                raise OperatorAccessError(
                    "operator_permission_denied", "准备命令缺少同源可信准入", 403
                ) from None
            prior = await session.scalar(
                select(HumanPreparationCommandRow).where(
                    HumanPreparationCommandRow.authorization_id == authorization_id
                )
            )
            if prior is not None:
                audit = await validated_audit(session, prior)
                if audit.investigation_run_id != run_id or audit.command_sha256 != content_hash(
                    command
                ):
                    raise OperatorAccessError(
                        "operator_permission_denied", "准入已绑定另一准备命令", 403
                    )
                if audit.outcome == "error":
                    self._check(principal)
                    return PreparationSubmission(
                        error=PreparationError(
                            audit.error_code or "human_preparation_conflict",
                            audit.error_message or "准备冲突",
                            audit.http_status,
                        )
                    )
                run = await lock_wait_run(session, run_id, read=True)
                job = await session.get(HumanPreparationJobRow, audit.job_id)
                assert job is not None
                view = await read_job(session, job, run, clock=self._clock)
                self._check(principal)
                return PreparationSubmission(result=PreparationResult(applied=False, job=view))
            try:
                run = await lock_wait_run(session, run_id, read=False)
                self._check(principal)
                job = await session.scalar(
                    select(HumanPreparationJobRow).where(
                        HumanPreparationJobRow.investigation_run_id == run_id,
                        HumanPreparationJobRow.requested_by_id == principal.subject_id,
                        HumanPreparationJobRow.idempotency_key == command.idempotency_key,
                    )
                )
                applied = job is None
                if job is not None:
                    if not source_matches_command(job, command):
                        raise PreparationError(
                            "human_preparation_idempotency_conflict", "幂等键已绑定另一完整准备命令"
                        )
                    view = await read_job(session, job, run, clock=self._clock)
                else:
                    handoff, wait = await load_wait_state(session, run_id)
                    if (
                        command.handoff_id != handoff.id
                        or command.source_sha256 != handoff.source_sha256
                    ):
                        raise PreparationError(
                            "human_preparation_source_conflict", "准备命令不匹配精确交接来源"
                        )
                    if wait.status != "waiting" or run.status != "await_human":
                        raise PreparationError(
                            "human_preparation_not_available", "本次等待已取消或不再可准备"
                        )
                    if wait.state_version != command.expected_wait_version:
                        raise PreparationError(
                            "human_preparation_version_conflict", "准备所需等待版本已变化"
                        )
                    if not wait.events:
                        raise PreparationError(
                            "human_preparation_input_required", "至少补充一项人工材料后才能准备"
                        )
                    occupied = await session.scalar(
                        select(HumanPreparationJobRow.id).where(
                            HumanPreparationJobRow.investigation_run_id == run_id,
                            HumanPreparationJobRow.wait_version == wait.state_version,
                        )
                    )
                    if occupied is not None:
                        raise PreparationError(
                            "human_preparation_already_exists", "当前等待版本已创建准备任务"
                        )
                    incident = await session.scalar(
                        select(IncidentRow)
                        .where(IncidentRow.id == run.incident_id)
                        .with_for_update()
                    )
                    if incident is None or incident.status != "investigating":
                        raise PreparationError(
                            "human_preparation_not_available", "事故不再允许准备"
                        )
                    source = await load_preparation_source(
                        session, run, wait_version=wait.state_version
                    )
                    payload = source.model_dump(mode="json")
                    if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > 524288:
                        raise PreparationError(
                            "human_preparation_source_too_large", "完整准备来源超过保存上限"
                        )
                    now = self._check(principal)
                    if now < wait.events[-1].created_at:
                        raise PreparationError(
                            "human_preparation_time_conflict", "准备不能早于已保存补充"
                        )
                    job = HumanPreparationJobRow(
                        id=uuid5(run_id, f"human-prepare:{wait.state_version}"),
                        investigation_run_id=run_id,
                        handoff_id=handoff.id,
                        wait_version=wait.state_version,
                        policy_version=POLICY_VERSION,
                        source=payload,
                        source_sha256=content_hash(source),
                        requested_by_id=principal.subject_id,
                        authorization_id=authorization_id,
                        idempotency_key=command.idempotency_key,
                        command_sha256=content_hash(command),
                        status="queued",
                        attempt_count=0,
                        available_at=now,
                        locked_by=None,
                        lease_token=None,
                        locked_until=None,
                        last_error_code=None,
                        created_at=now,
                        updated_at=now,
                        completed_at=None,
                    )
                    session.add(job)
                    await session.flush()
            except (PreparationError, HumanWaitError, TaskFactConflict) as error:
                failure = (
                    error
                    if isinstance(error, PreparationError)
                    else PreparationError(
                        "human_preparation_run_not_found"
                        if isinstance(error, HumanWaitError) and error.status_code == 404
                        else "human_preparation_source_conflict",
                        str(error),
                        error.status_code if isinstance(error, HumanWaitError) else 409,
                    )
                )
                _record(
                    session,
                    run_id,
                    principal.subject_id,
                    command,
                    raw,
                    authorization_id,
                    now=self._check(principal),
                    error=failure,
                )
                await session.flush()
                self._check(principal)
                return PreparationSubmission(error=failure)
            assert job is not None
            _record(
                session,
                run_id,
                principal.subject_id,
                command,
                raw,
                authorization_id,
                now=now if applied else self._check(principal),
                job_id=job.id,
                applied=applied,
            )
            await session.flush()
            if applied:
                view = await read_job(session, job, run, clock=self._clock)
            self._check(principal)
            return PreparationSubmission(result=PreparationResult(applied=applied, job=view))

    async def get(self, run_id: UUID, job_id: UUID) -> PreparationJobView:
        async with self._factory() as session, session.begin():
            run = await lock_wait_run(session, run_id, read=True)
            job = await session.scalar(
                select(HumanPreparationJobRow)
                .where(
                    HumanPreparationJobRow.id == job_id,
                    HumanPreparationJobRow.investigation_run_id == run_id,
                )
                .with_for_update(read=True)
            )
            if job is None:
                raise PreparationError("human_preparation_job_not_found", "准备任务不存在", 404)
            return await read_job(session, job, run, clock=self._clock)

    async def list_commands(
        self, run_id: UUID, *, after_cursor: int = 0, limit: int = 50
    ) -> PreparationAuditPage:
        if after_cursor < 0 or not 1 <= limit <= 100:
            raise ValueError("准备审计分页参数不合法")
        async with self._factory() as session, session.begin():
            run = await lock_wait_run(session, run_id, read=True)
            rows = list(
                await session.scalars(
                    select(HumanPreparationCommandRow)
                    .where(
                        HumanPreparationCommandRow.investigation_run_id == run_id,
                        HumanPreparationCommandRow.cursor > after_cursor,
                    )
                    .order_by(HumanPreparationCommandRow.cursor)
                    .limit(limit + 1)
                )
            )
            records = tuple([await validated_audit(session, row) for row in rows[:limit]])
            for job_id in {item.job_id for item in records if item.job_id is not None}:
                job = await session.get(HumanPreparationJobRow, job_id)
                assert job is not None
                await read_job(session, job, run, clock=self._clock)
            return PreparationAuditPage(
                items=records, next_cursor=records[-1].cursor if len(rows) > limit else None
            )


def _record(
    session: AsyncSession,
    run_id: UUID,
    subject_id: str,
    command: PrepareCommand,
    raw_body: str,
    authorization_id: UUID,
    *,
    now: datetime,
    job_id: UUID | None = None,
    applied: bool = False,
    error: PreparationError | None = None,
) -> None:
    session.add(
        HumanPreparationCommandRow(
            authorization_id=authorization_id,
            investigation_run_id=run_id,
            subject_id=subject_id,
            command=command.model_dump(mode="json"),
            raw_body=raw_body,
            command_sha256=content_hash(command),
            outcome="error" if error else "applied" if applied else "replayed",
            http_status=error.status_code if error else 201 if applied else 200,
            job_id=job_id,
            error_code=error.code if error else None,
            error_message=str(error) if error else None,
            created_at=now,
        )
    )
