"""可信经验替代的有序双行锁、原子事实及独立业务结果。"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.experience.contracts import ExperienceError, canonical_hash
from opsswarm.experience.replacement_contracts import (
    REPLACEMENT_POLICY_VERSION,
    ReplaceCommand,
    ReplacementView,
    ReplaceResult,
)
from opsswarm.experience.replacement_policy import replacement_action, validate_replacement
from opsswarm.experience.replacement_tables import (
    ExperienceReplacementCommandRow,
    ExperienceReplacementRow,
)
from opsswarm.experience.replacement_validation import (
    replacement_id_for_entry,
    validated_replacement,
)
from opsswarm.experience.review_store import load_confirmed_entry_in_session
from opsswarm.experience.review_tables import ExperienceEntryRow
from opsswarm.experience.state_reader import load_entry_state
from opsswarm.experience.store import utc_now
from opsswarm.identity.models import OperatorAccessError, OperatorPrincipal, require_permission
from opsswarm.identity.tables import OperatorAuthorizationRow


@dataclass(frozen=True)
class ReplaceOutcome:
    result: ReplaceResult | None = None
    error: ExperienceError | None = None


class ExperienceReplacementStore:
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
            raise ValueError("替代时间必须包含时区")
        return value.astimezone(UTC)

    def _check_expiry(self, principal: OperatorPrincipal) -> None:
        if self._now() >= principal.expires_at:
            raise OperatorAccessError("operator_unauthenticated", "操作者认证已经过期")

    async def replace(
        self,
        entry_id: UUID,
        command: ReplaceCommand,
        *,
        principal: OperatorPrincipal,
        authorization_id: UUID,
    ) -> ReplaceOutcome:
        require_permission(principal, "POST", "replace_experience_entry")
        self._check_expiry(principal)
        async with self._factory() as session, session.begin():
            admission = await session.get(OperatorAuthorizationRow, authorization_id)
            if (
                admission is None
                or admission.subject_id != principal.subject_id
                or admission.credential_id != principal.credential_id
                or admission.role != principal.role.value
                or admission.method != "POST"
                or admission.operation != "replace_experience_entry"
                or admission.resources != {"entry_id": str(entry_id)}
                or "manage_report" not in admission.permissions
            ):
                raise OperatorAccessError("operator_permission_denied", "替代缺少同源授权准入", 403)
            existing: ReplacementView | None = None
            try:
                await lock_replacement_entries(session, entry_id, command.target_entry_id)
                self._check_expiry(principal)
                entry = await load_confirmed_entry_in_session(session, entry_id)
                state = await load_entry_state(session, entry)
                existing = state.replacement
                action = replacement_action(
                    command, subject_id=principal.subject_id, existing=existing
                )
                if action == "apply":
                    target = await load_confirmed_entry_in_session(session, command.target_entry_id)
                    target_state = await load_entry_state(session, target)
                    predecessor = await session.scalar(
                        select(ExperienceReplacementRow.id).where(
                            ExperienceReplacementRow.target_entry_id == target.id
                        )
                    )
                    validate_replacement(
                        entry,
                        target,
                        command,
                        entry_active=state.status == "active",
                        target_active=target_state.status == "active",
                        target_has_predecessor=predecessor is not None,
                    )
                    now = self._now()
                    if now < target.created_at:
                        raise ExperienceError(
                            "experience_replace_time_conflict", "替代时间早于目标确认"
                        )
                    async with session.begin_nested():
                        row = ExperienceReplacementRow(
                            id=replacement_id_for_entry(entry.id),
                            entry_id=entry.id,
                            entry_version=entry.version,
                            entry_content_hash=entry.content_hash,
                            source_hash=entry.source_hash,
                            target_entry_id=target.id,
                            target_entry_version=target.version,
                            target_content_hash=target.content_hash,
                            target_source_hash=target.source_hash,
                            state_version=2,
                            policy_version=REPLACEMENT_POLICY_VERSION,
                            replaced_by_id=principal.subject_id,
                            authorization_id=admission.id,
                            idempotency_key=command.idempotency_key,
                            command_hash=canonical_hash(command),
                            reason=command.reason,
                            replaced_at=now,
                        )
                        session.add(row)
                        await session.flush()
                        existing = validated_replacement(row, entry, target, admission)
                assert existing is not None
                result = ReplaceResult(applied=action == "apply", replacement=existing)
                _record(
                    session,
                    entry_id,
                    command,
                    principal.subject_id,
                    authorization_id,
                    now=self._now(),
                    result=result,
                )
                return ReplaceOutcome(result=result)
            except ExperienceError as error:
                _record(
                    session,
                    entry_id,
                    command,
                    principal.subject_id,
                    authorization_id,
                    now=self._now(),
                    error=error,
                    replacement_id=existing.id if existing else None,
                )
                return ReplaceOutcome(error=error)


async def lock_replacement_entries(session: AsyncSession, entry_id: UUID, target_id: UUID) -> None:
    # 与检索和数据库插入守卫使用同一顺序；不存在的目标由业务层在审计事务内返回404。
    for selected_id in sorted({entry_id, target_id}, key=lambda value: value.int):
        await session.scalar(
            select(ExperienceEntryRow.id)
            .where(ExperienceEntryRow.id == selected_id)
            .with_for_update()
        )


def _record(
    session: AsyncSession,
    entry_id: UUID,
    command: ReplaceCommand,
    subject_id: str,
    authorization_id: UUID,
    *,
    now: datetime,
    result: ReplaceResult | None = None,
    error: ExperienceError | None = None,
    replacement_id: UUID | None = None,
) -> None:
    session.add(
        ExperienceReplacementCommandRow(
            id=uuid4(),
            authorization_id=authorization_id,
            entry_id=entry_id,
            target_entry_id=command.target_entry_id,
            requested_by_id=subject_id,
            command=command.model_dump(mode="json"),
            command_hash=canonical_hash(command),
            outcome="error" if error else "applied" if result and result.applied else "replayed",
            error_code=error.code if error else None,
            http_status=error.status_code if error else 201 if result and result.applied else 200,
            replacement_id=result.replacement.id if result else replacement_id,
            created_at=now,
        )
    )
