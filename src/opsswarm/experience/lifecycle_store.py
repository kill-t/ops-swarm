"""有效性投影、可信停用和独立业务结果的原子持久用例。"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.experience.contracts import ExperienceError, canonical_hash
from opsswarm.experience.lifecycle_contracts import (
    LIFECYCLE_POLICY_VERSION,
    DisableCommand,
    DisablementView,
    DisableResult,
    EntryState,
)
from opsswarm.experience.lifecycle_policy import evaluate_disable
from opsswarm.experience.lifecycle_tables import (
    ExperienceDisablementRow,
    ExperienceLifecycleCommandRow,
)
from opsswarm.experience.lifecycle_validation import disablement_id_for_entry, validated_disablement
from opsswarm.experience.review_contracts import EntryView
from opsswarm.experience.review_store import load_confirmed_entry_in_session
from opsswarm.experience.review_tables import ExperienceEntryRow
from opsswarm.experience.store import utc_now
from opsswarm.identity.models import OperatorAccessError, OperatorPrincipal, require_permission
from opsswarm.identity.tables import OperatorAuthorizationRow


@dataclass(frozen=True)
class DisableOutcome:
    result: DisableResult | None = None
    error: ExperienceError | None = None


class ExperienceLifecycleStore:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._factory = session_factory
        self._clock = clock

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("停用时间必须包含时区")
        return value.astimezone(UTC)

    async def disable(
        self,
        entry_id: UUID,
        command: DisableCommand,
        *,
        principal: OperatorPrincipal,
        authorization_id: UUID,
    ) -> DisableOutcome:
        require_permission(principal, "POST", "disable_experience_entry")
        self._check_expiry(principal)
        async with self._factory() as session, session.begin():
            admission = await session.get(OperatorAuthorizationRow, authorization_id)
            if (
                admission is None
                or admission.subject_id != principal.subject_id
                or admission.credential_id != principal.credential_id
                or admission.role != principal.role.value
                or admission.method != "POST"
                or admission.operation != "disable_experience_entry"
                or admission.resources != {"entry_id": str(entry_id)}
                or "manage_report" not in admission.permissions
            ):
                raise OperatorAccessError("operator_permission_denied", "停用缺少同源授权准入", 403)
            existing: DisablementView | None = None
            try:
                entry = await _locked_entry(session, entry_id, read=False)
                self._check_expiry(principal)
                existing = await _disablement(session, entry)
                action = evaluate_disable(
                    entry, command, subject_id=principal.subject_id, existing=existing
                )
                if action == "apply":
                    now = self._now()
                    if now < entry.created_at:
                        raise ExperienceError(
                            "experience_disable_time_conflict", "停用时间早于确认时间"
                        )
                    async with session.begin_nested():
                        row = ExperienceDisablementRow(
                            id=disablement_id_for_entry(entry.id),
                            entry_id=entry.id,
                            entry_version=entry.version,
                            entry_content_hash=entry.content_hash,
                            source_hash=entry.source_hash,
                            state_version=2,
                            policy_version=LIFECYCLE_POLICY_VERSION,
                            disabled_by_id=principal.subject_id,
                            authorization_id=admission.id,
                            idempotency_key=command.idempotency_key,
                            command_hash=canonical_hash(command),
                            reason=command.reason,
                            disabled_at=now,
                        )
                        session.add(row)
                        await session.flush()
                        existing = validated_disablement(row, entry, admission)
                assert existing is not None
                result = DisableResult(applied=action == "apply", state=_state(entry, existing))
                _record(
                    session,
                    entry_id,
                    command,
                    principal.subject_id,
                    authorization_id,
                    now=self._now(),
                    result=result,
                )
                return DisableOutcome(result=result)
            except ExperienceError as error:
                _record(
                    session,
                    entry_id,
                    command,
                    principal.subject_id,
                    authorization_id,
                    now=self._now(),
                    error=error,
                    disablement_id=existing.id if existing else None,
                )
                return DisableOutcome(error=error)

    def _check_expiry(self, principal: OperatorPrincipal) -> None:
        if self._now() >= principal.expires_at:
            raise OperatorAccessError("operator_unauthenticated", "操作者认证已经过期")

    async def get_state(self, entry_id: UUID) -> EntryState:
        async with self._factory() as session, session.begin():
            entry = await _locked_entry(session, entry_id, read=True)
            return _state(entry, await _disablement(session, entry))


async def _locked_entry(session: AsyncSession, entry_id: UUID, *, read: bool) -> EntryView:
    row = await session.scalar(
        select(ExperienceEntryRow)
        .where(
            ExperienceEntryRow.id == entry_id,
        )
        .with_for_update(read=read)
    )
    if row is None:
        raise ExperienceError("experience_entry_not_found", "已确认经验版本不存在", 404)
    return await load_confirmed_entry_in_session(session, entry_id)


async def _disablement(session: AsyncSession, entry: EntryView) -> DisablementView | None:
    row = await session.scalar(
        select(ExperienceDisablementRow).where(
            ExperienceDisablementRow.entry_id == entry.id,
        )
    )
    if row is None:
        return None
    admission = await session.get(OperatorAuthorizationRow, row.authorization_id)
    return validated_disablement(row, entry, admission)


def _state(entry: EntryView, disablement: DisablementView | None) -> EntryState:
    return EntryState(
        entry_id=entry.id,
        entry_version=entry.version,
        entry_content_hash=entry.content_hash,
        state_version=1 if disablement is None else 2,
        status="active" if disablement is None else "disabled",
        disablement=disablement,
    )


def _record(
    session: AsyncSession,
    entry_id: UUID,
    command: DisableCommand,
    subject_id: str,
    authorization_id: UUID,
    *,
    now: datetime,
    result: DisableResult | None = None,
    error: ExperienceError | None = None,
    disablement_id: UUID | None = None,
) -> None:
    session.add(
        ExperienceLifecycleCommandRow(
            id=uuid4(),
            authorization_id=authorization_id,
            entry_id=entry_id,
            requested_by_id=subject_id,
            command=command.model_dump(mode="json"),
            command_hash=canonical_hash(command),
            outcome="error" if error else "applied" if result and result.applied else "replayed",
            error_code=error.code if error else None,
            http_status=error.status_code if error else 201 if result and result.applied else 200,
            disablement_id=(
                result.state.disablement.id
                if result and result.state.disablement
                else disablement_id
            ),
            created_at=now,
        )
    )
