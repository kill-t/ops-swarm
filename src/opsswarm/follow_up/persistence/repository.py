"""复盘改进行动项的 PostgreSQL 仓储与事务边界。"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.follow_up.errors import (
    FollowUpReportNotFound,
    ReportActionItemIdempotencyConflict,
    ReportActionItemNotFound,
    ReportActionItemValidationError,
)
from opsswarm.follow_up.models import (
    ReportActionItem,
    ReportActionItemCompletion,
    ReportActionItemCreation,
    ReportActionItemEvent,
    ReportActionItemEventType,
    ReportActionItemGovernanceEntry,
    ReportActionItemGovernancePage,
    ReportActionItemPage,
    ReportActionItemStatus,
)
from opsswarm.follow_up.persistence.tables import (
    ReportActionItemEventRow,
    ReportActionItemRow,
)
from opsswarm.follow_up.state_machine import (
    complete_report_action_item,
    create_report_action_item,
)
from opsswarm.incidents import IncidentSeverity
from opsswarm.incidents.persistence.tables import IncidentRow
from opsswarm.reports.persistence.tables import IncidentReportRow


class ReportActionItemStore:
    """持久化人工创建的改进行动项和不可变状态事件。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create(
        self,
        report_id: UUID,
        *,
        idempotency_key: str,
        title: str,
        description: str,
        owner_id: str,
        due_at: datetime,
        created_by_id: str,
        occurred_at: datetime,
    ) -> ReportActionItemCreation:
        """在报告锁下幂等创建 open 行动项及首条审计事件。"""

        candidate = create_report_action_item(
            item_id=uuid4(),
            report_id=report_id,
            idempotency_key=idempotency_key,
            title=title,
            description=description,
            owner_id=owner_id,
            due_at=due_at,
            created_by_id=created_by_id,
            occurred_at=occurred_at,
        )
        async with self._session_factory() as session, session.begin():
            report = await session.scalar(
                select(IncidentReportRow)
                .where(IncidentReportRow.id == report_id)
                .with_for_update()
            )
            if report is None:
                raise FollowUpReportNotFound(report_id)
            existing = await session.scalar(
                select(ReportActionItemRow).where(
                    ReportActionItemRow.report_id == report_id,
                    ReportActionItemRow.idempotency_key == candidate.idempotency_key,
                )
            )
            if existing is not None:
                stored = _action_item(existing)
                if not _same_creation(stored, candidate):
                    raise ReportActionItemIdempotencyConflict(
                        report_id,
                        candidate.idempotency_key,
                    )
                return ReportActionItemCreation(created=False, item=stored)

            row = _action_item_row(candidate)
            session.add(row)
            await session.flush()
            session.add(
                ReportActionItemEventRow(
                    action_item_id=candidate.id,
                    event_type=ReportActionItemEventType.CREATED.value,
                    actor_id=candidate.created_by_id,
                    from_status=None,
                    to_status=ReportActionItemStatus.OPEN.value,
                    reason="创建复盘改进行动项",
                    item_version=1,
                    occurred_at=candidate.created_at,
                )
            )
            await session.flush()
            return ReportActionItemCreation(created=True, item=_action_item(row))

    async def list_for_report(
        self,
        report_id: UUID,
        *,
        limit: int = 100,
    ) -> ReportActionItemPage:
        """待完成优先、按截止时间读取一个报告的有界行动项。"""

        if isinstance(limit, bool) or not 1 <= limit <= 200:
            raise ValueError("limit 必须在 1 到 200 之间")
        async with self._session_factory() as session:
            if await session.get(IncidentReportRow, report_id) is None:
                raise FollowUpReportNotFound(report_id)
            total = int(
                await session.scalar(
                    select(func.count())
                    .select_from(ReportActionItemRow)
                    .where(ReportActionItemRow.report_id == report_id)
                )
                or 0
            )
            rows = tuple(
                await session.scalars(
                    select(ReportActionItemRow)
                    .where(ReportActionItemRow.report_id == report_id)
                    .order_by(
                        case((ReportActionItemRow.status == "open", 0), else_=1),
                        ReportActionItemRow.due_at,
                        ReportActionItemRow.created_at,
                        ReportActionItemRow.id,
                    )
                    .limit(limit)
                )
            )
            return ReportActionItemPage(
                items=tuple(_action_item(row) for row in rows),
                total=total,
            )

    async def list_open_governance(
        self,
        *,
        as_of: datetime,
        owner_id: str | None = None,
        overdue_only: bool = False,
        limit: int = 100,
    ) -> ReportActionItemGovernancePage:
        """按服务器时间读取跨事故 open 待办，不产生任何状态变化。"""

        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ReportActionItemValidationError("as_of 必须包含时区")
        normalized_as_of = as_of.astimezone(UTC)
        if isinstance(limit, bool) or not 1 <= limit <= 200:
            raise ReportActionItemValidationError("limit 必须在 1 到 200 之间")
        normalized_owner_id = owner_id.strip() if owner_id is not None else None
        if normalized_owner_id == "":
            raise ReportActionItemValidationError("owner_id 不能为空")

        conditions = [ReportActionItemRow.status == ReportActionItemStatus.OPEN.value]
        if normalized_owner_id is not None:
            conditions.append(ReportActionItemRow.owner_id == normalized_owner_id)
        if overdue_only:
            conditions.append(ReportActionItemRow.due_at < normalized_as_of)

        join_conditions = (
            ReportActionItemRow.report_id == IncidentReportRow.id,
            IncidentReportRow.incident_id == IncidentRow.id,
        )
        async with self._session_factory() as session:
            total = int(
                await session.scalar(
                    select(func.count())
                    .select_from(ReportActionItemRow)
                    .join(IncidentReportRow, join_conditions[0])
                    .join(IncidentRow, join_conditions[1])
                    .where(*conditions)
                )
                or 0
            )
            rows = (
                await session.execute(
                    select(ReportActionItemRow, IncidentRow)
                    .join(IncidentReportRow, join_conditions[0])
                    .join(IncidentRow, join_conditions[1])
                    .where(*conditions)
                    .order_by(
                        ReportActionItemRow.due_at,
                        IncidentRow.display_number,
                        ReportActionItemRow.id,
                    )
                    .limit(limit)
                )
            ).tuples()
            return ReportActionItemGovernancePage(
                items=tuple(
                    ReportActionItemGovernanceEntry(
                        action_item=_action_item(item_row),
                        incident_id=incident_row.id,
                        incident_display_number=incident_row.display_number,
                        incident_service_name=incident_row.service_name,
                        incident_title=incident_row.title,
                        incident_severity=IncidentSeverity(incident_row.severity),
                        overdue=item_row.due_at < normalized_as_of,
                    )
                    for item_row, incident_row in rows
                ),
                total=total,
                as_of=normalized_as_of,
                owner_id=normalized_owner_id,
                overdue_only=overdue_only,
            )

    async def complete(
        self,
        item_id: UUID,
        *,
        expected_version: int,
        completed_by_id: str,
        completion_note: str,
        occurred_at: datetime,
    ) -> ReportActionItemCompletion:
        """锁定行动项后执行纯状态迁移，并原子追加完成事件。"""

        async with self._session_factory() as session, session.begin():
            row = await session.scalar(
                select(ReportActionItemRow)
                .where(ReportActionItemRow.id == item_id)
                .with_for_update()
            )
            if row is None:
                raise ReportActionItemNotFound(item_id)
            completed, applied = complete_report_action_item(
                _action_item(row),
                expected_version=expected_version,
                completed_by_id=completed_by_id,
                completion_note=completion_note,
                occurred_at=occurred_at,
            )
            if not applied:
                return ReportActionItemCompletion(completion_applied=False, item=completed)

            # 领域模型已保证完成态三个字段同时存在，这里显式收窄类型，避免持久层写入伪造空值。
            assert completed.completed_by_id is not None
            assert completed.completion_note is not None
            assert completed.completed_at is not None

            row.status = completed.status.value
            row.version = completed.version
            row.completed_by_id = completed.completed_by_id
            row.completion_note = completed.completion_note
            row.completed_at = completed.completed_at
            session.add(
                ReportActionItemEventRow(
                    action_item_id=completed.id,
                    event_type=ReportActionItemEventType.COMPLETED.value,
                    actor_id=completed.completed_by_id,
                    from_status=ReportActionItemStatus.OPEN.value,
                    to_status=ReportActionItemStatus.COMPLETED.value,
                    reason=completed.completion_note,
                    item_version=completed.version,
                    occurred_at=completed.completed_at,
                )
            )
            await session.flush()
            return ReportActionItemCompletion(completion_applied=True, item=_action_item(row))

    async def list_events(self, item_id: UUID) -> tuple[ReportActionItemEvent, ...]:
        """按全局游标读取行动项完整审计时间线。"""

        async with self._session_factory() as session:
            if await session.get(ReportActionItemRow, item_id) is None:
                raise ReportActionItemNotFound(item_id)
            rows = tuple(
                await session.scalars(
                    select(ReportActionItemEventRow)
                    .where(ReportActionItemEventRow.action_item_id == item_id)
                    .order_by(ReportActionItemEventRow.id)
                )
            )
            return tuple(_action_item_event(row) for row in rows)


def _same_creation(existing: ReportActionItem, candidate: ReportActionItem) -> bool:
    return (
        existing.title == candidate.title
        and existing.description == candidate.description
        and existing.owner_id == candidate.owner_id
        and existing.due_at == candidate.due_at
        and existing.created_by_id == candidate.created_by_id
    )


def _action_item_row(item: ReportActionItem) -> ReportActionItemRow:
    return ReportActionItemRow(
        id=item.id,
        report_id=item.report_id,
        idempotency_key=item.idempotency_key,
        title=item.title,
        description=item.description,
        owner_id=item.owner_id,
        status=item.status.value,
        version=item.version,
        due_at=item.due_at,
        created_by_id=item.created_by_id,
        created_at=item.created_at,
        completed_by_id=item.completed_by_id,
        completion_note=item.completion_note,
        completed_at=item.completed_at,
    )


def _action_item(row: ReportActionItemRow) -> ReportActionItem:
    return ReportActionItem(
        id=row.id,
        report_id=row.report_id,
        idempotency_key=row.idempotency_key,
        title=row.title,
        description=row.description,
        owner_id=row.owner_id,
        status=ReportActionItemStatus(row.status),
        version=row.version,
        due_at=row.due_at,
        created_by_id=row.created_by_id,
        created_at=row.created_at,
        completed_by_id=row.completed_by_id,
        completion_note=row.completion_note,
        completed_at=row.completed_at,
    )


def _action_item_event(row: ReportActionItemEventRow) -> ReportActionItemEvent:
    return ReportActionItemEvent(
        cursor=row.id,
        item_id=row.action_item_id,
        event_type=ReportActionItemEventType(row.event_type),
        actor_id=row.actor_id,
        from_status=(
            ReportActionItemStatus(row.from_status) if row.from_status is not None else None
        ),
        to_status=ReportActionItemStatus(row.to_status),
        reason=row.reason,
        item_version=row.item_version,
        occurred_at=row.occurred_at,
    )
