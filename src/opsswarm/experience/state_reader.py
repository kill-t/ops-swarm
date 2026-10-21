"""在调用方持有 entry 锁的事务内复核互斥终态及直接后继。"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from opsswarm.experience.contracts import ExperienceError
from opsswarm.experience.lifecycle_contracts import DisablementView, EntryState
from opsswarm.experience.lifecycle_tables import ExperienceDisablementRow
from opsswarm.experience.lifecycle_validation import validated_disablement
from opsswarm.experience.replacement_contracts import ReplacementView
from opsswarm.experience.replacement_tables import ExperienceReplacementRow
from opsswarm.experience.replacement_validation import validated_replacement
from opsswarm.experience.review_contracts import EntryView
from opsswarm.experience.review_store import load_confirmed_entry_in_session
from opsswarm.identity.tables import OperatorAuthorizationRow


async def load_replacement(session: AsyncSession, entry: EntryView) -> ReplacementView | None:
    row = await session.scalar(
        select(ExperienceReplacementRow).where(ExperienceReplacementRow.entry_id == entry.id)
    )
    if row is None:
        return None
    target = await load_confirmed_entry_in_session(session, row.target_entry_id)
    admission = await session.get(OperatorAuthorizationRow, row.authorization_id)
    # 这里只验证不可变工件，不能因后继后来停用而使原关系失效。
    return validated_replacement(row, entry, target, admission)


async def load_entry_state(session: AsyncSession, entry: EntryView) -> EntryState:
    row = await session.scalar(
        select(ExperienceDisablementRow).where(ExperienceDisablementRow.entry_id == entry.id)
    )
    disablement = None
    if row is not None:
        admission = await session.get(OperatorAuthorizationRow, row.authorization_id)
        disablement = validated_disablement(row, entry, admission)
    return project_entry_state(entry, disablement, await load_replacement(session, entry))


def project_entry_state(
    entry: EntryView,
    disablement: DisablementView | None,
    replacement: ReplacementView | None = None,
) -> EntryState:
    if disablement is not None and replacement is not None:
        raise ExperienceError("experience_state_integrity_conflict", "经验存在互斥的双重终态")
    return EntryState(
        entry_id=entry.id,
        entry_version=entry.version,
        entry_content_hash=entry.content_hash,
        state_version=2 if disablement or replacement else 1,
        status="disabled" if disablement else "replaced" if replacement else "active",
        disablement=disablement,
        replacement=replacement,
    )
