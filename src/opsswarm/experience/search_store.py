"""按持久调查范围检索最多三个可用经验，并复核停用和确认工件。"""

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import String, case, exists, literal_column, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.experience.contracts import ExperienceError
from opsswarm.experience.lifecycle_contracts import (
    ExperienceSearchQuery,
    ExperienceSearchResult,
    SearchScope,
)
from opsswarm.experience.lifecycle_tables import ExperienceDisablementRow
from opsswarm.experience.replacement_tables import ExperienceReplacementRow
from opsswarm.experience.review_store import load_confirmed_entry_in_session
from opsswarm.experience.review_tables import ExperienceEntryRow
from opsswarm.experience.search_policy import summarize_entry
from opsswarm.experience.store import utc_now
from opsswarm.experience.tables import ExperienceCandidateRow, ExperienceJobRow
from opsswarm.incidents.persistence.tables import IncidentRow
from opsswarm.investigation.models import InvestigationDataSourceMode
from opsswarm.investigation.persistence.tables import InvestigationRunRow


class ExperienceSearchStore:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._factory = session_factory
        self._clock = clock

    async def search(self, run_id: UUID, query: ExperienceSearchQuery) -> ExperienceSearchResult:
        async with self._factory() as session, session.begin():
            run = await session.get(InvestigationRunRow, run_id)
            if run is None:
                raise ExperienceError("investigation_run_not_found", "调查运行不存在", 404)
            incident = await session.get(IncidentRow, run.incident_id)
            if incident is None:
                raise ExperienceError("experience_search_scope_conflict", "调查没有有效事故范围")
            scope = SearchScope(
                run_id=run.id,
                incident_id=incident.id,
                service_name=incident.service_name,
                data_source_mode=InvestigationDataSourceMode(run.data_source_mode),
            )
            # 固定 JSON 路径使用与迁移索引一致的运算符，不把调用方字符串拼入 SQL。
            draft = ExperienceEntryRow.content.op("->")(literal_column("'candidate_snapshot'"))
            ordering = []
            if query.preferred_fault_tag is not None:
                ordering.append(
                    case(
                        (
                            draft.op("->")(literal_column("'fault_tags'")).contains(
                                [query.preferred_fault_tag.value]
                            ),
                            1,
                        ),
                        else_=0,
                    ).desc()
                )
            ordering.extend((ExperienceEntryRow.created_at.desc(), ExperienceEntryRow.id.asc()))
            source_incident = ExperienceJobRow.source["incident_id"].astext
            rows = (
                await session.execute(
                    select(ExperienceEntryRow, source_incident)
                    .join(
                        ExperienceCandidateRow,
                        ExperienceEntryRow.candidate_id == ExperienceCandidateRow.id,
                    )
                    .join(ExperienceJobRow, ExperienceCandidateRow.job_id == ExperienceJobRow.id)
                    .where(
                        ExperienceEntryRow.status == "confirmed",
                        source_incident != str(incident.id),
                        draft.op("->>", return_type=String)(literal_column("'service_name'"))
                        == scope.service_name,
                        draft.op("->>", return_type=String)(literal_column("'environment'"))
                        == scope.environment,
                        draft.op("->>", return_type=String)(literal_column("'data_source_mode'"))
                        == scope.data_source_mode.value,
                        ~exists().where(ExperienceDisablementRow.entry_id == ExperienceEntryRow.id),
                        ~exists().where(ExperienceReplacementRow.entry_id == ExperienceEntryRow.id),
                    )
                    .order_by(*ordering)
                    .limit(3)
                )
            ).all()
            await lock_search_entries(session, [row.id for row, _ in rows])
            # 拿到全部行锁后另开语句读取停用事实，不能依赖等待锁前的子查询快照。
            disabled_ids = set(
                await session.scalars(
                    select(ExperienceDisablementRow.entry_id).where(
                        ExperienceDisablementRow.entry_id.in_([row.id for row, _ in rows]),
                    )
                )
            )
            replaced_ids = set(
                await session.scalars(
                    select(ExperienceReplacementRow.entry_id).where(
                        ExperienceReplacementRow.entry_id.in_([row.id for row, _ in rows]),
                    )
                )
            )
            items = []
            for row, source_incident_id in rows:
                if row.id in disabled_ids or row.id in replaced_ids:
                    continue
                entry = await load_confirmed_entry_in_session(session, row.id)
                items.append(summarize_entry(entry, UUID(source_incident_id), scope))
            now = self._clock()
            if now.tzinfo is None or now.utcoffset() is None:
                raise ValueError("检索时间必须包含时区")
            return ExperienceSearchResult(
                scope=scope,
                preferred_fault_tag=query.preferred_fault_tag,
                retrieved_at=now.astimezone(UTC),
                items=tuple(items),
            )


async def lock_search_entries(session: AsyncSession, entry_ids: list[UUID]) -> None:
    # 排名与锁顺序分开，避免与双侧替代锁产生反向等待。
    for entry_id in sorted(entry_ids, key=lambda value: value.int):
        locked_id = await session.scalar(
            select(ExperienceEntryRow.id)
            .where(ExperienceEntryRow.id == entry_id)
            .with_for_update(read=True)
        )
        if locked_id is None:
            raise ExperienceError("experience_entry_integrity_conflict", "选中确认版本已不存在")
