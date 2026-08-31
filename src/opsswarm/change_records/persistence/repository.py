"""变更记录的追加写入、幂等和有界查询仓储。"""

from collections.abc import Sequence
from datetime import datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.change_records.errors import ChangeRecordIdempotencyConflict
from opsswarm.change_records.models import (
    CHANGE_SOURCE,
    CHANGE_SPEC_ADAPTER,
    MAX_CHANGE_RECORDS,
    SUPPORTED_SERVICE,
    ChangeRecord,
    ChangeRecordPage,
    ChangeRecordWriteResult,
    ChangeSpec,
    change_content_hash,
)
from opsswarm.change_records.persistence.tables import ChangeRecordRow


class ChangeRecordStore:
    """只提供追加与只读查询，不暴露更新或物理删除能力。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def record(
        self,
        change: ChangeSpec,
        *,
        occurred_at: datetime,
        recorded_at: datetime,
    ) -> ChangeRecordWriteResult:
        """按来源与外部变更号幂等追加；内容漂移时拒绝复用。"""

        candidate = ChangeRecord(
            record_id=uuid4(),
            occurred_at=occurred_at,
            recorded_at=recorded_at,
            change=change,
        )
        content_hash = change_content_hash(change)
        values = {
            "id": candidate.record_id,
            "source": candidate.source,
            "external_change_id": change.external_change_id,
            "service_name": candidate.service_name,
            "change_type": change.change_type,
            "details": change.model_dump(mode="json", exclude_none=True),
            "content_hash": content_hash,
            "occurred_at": candidate.occurred_at,
            "recorded_at": candidate.recorded_at,
        }
        statement = (
            insert(ChangeRecordRow)
            .values(**values)
            .on_conflict_do_nothing(
                index_elements=[ChangeRecordRow.source, ChangeRecordRow.external_change_id]
            )
            .returning(ChangeRecordRow.id)
        )
        async with self._session_factory() as session, session.begin():
            created_id = await session.scalar(statement)
            if created_id is not None:
                return ChangeRecordWriteResult(record=candidate, deduplicated=False)

            existing = await session.scalar(
                select(ChangeRecordRow).where(
                    ChangeRecordRow.source == CHANGE_SOURCE,
                    ChangeRecordRow.external_change_id == change.external_change_id,
                )
            )
            if existing is None:
                raise RuntimeError("变更记录幂等冲突后未找到已有记录")
            if existing.content_hash != content_hash:
                raise ChangeRecordIdempotencyConflict(change.external_change_id)
            return ChangeRecordWriteResult(record=_record_from_row(existing), deduplicated=True)

    async def list_recent(
        self,
        *,
        service_name: str,
        since: datetime,
        until: datetime,
        limit: int = MAX_CHANGE_RECORDS,
    ) -> ChangeRecordPage:
        """按发生时间倒序返回有限结果，并显式报告是否截断。"""

        if service_name != SUPPORTED_SERVICE:
            raise ValueError("变更记录模拟器只支持 order-service")
        if since.tzinfo is None or since.utcoffset() is None:
            raise ValueError("since 必须包含时区")
        if until.tzinfo is None or until.utcoffset() is None:
            raise ValueError("until 必须包含时区")
        if since > until:
            raise ValueError("since 不能晚于 until")
        if not 1 <= limit <= MAX_CHANGE_RECORDS:
            raise ValueError(f"limit 必须在 1 到 {MAX_CHANGE_RECORDS} 之间")

        statement = (
            select(ChangeRecordRow)
            .where(
                ChangeRecordRow.service_name == service_name,
                ChangeRecordRow.occurred_at >= since,
                ChangeRecordRow.occurred_at <= until,
            )
            .order_by(ChangeRecordRow.occurred_at.desc(), ChangeRecordRow.id.desc())
            .limit(limit + 1)
        )
        async with self._session_factory() as session:
            rows: Sequence[ChangeRecordRow] = (await session.scalars(statement)).all()
        return ChangeRecordPage(
            items=tuple(_record_from_row(row) for row in rows[:limit]),
            truncated=len(rows) > limit,
        )


def _record_from_row(row: ChangeRecordRow) -> ChangeRecord:
    change = CHANGE_SPEC_ADAPTER.validate_python(row.details)
    if row.source != CHANGE_SOURCE:
        raise ValueError("变更记录来源不符合固定契约")
    if row.service_name != SUPPORTED_SERVICE:
        raise ValueError("变更记录服务不符合固定契约")
    if row.external_change_id != change.external_change_id:
        raise ValueError("变更记录外部编号与详情不一致")
    if row.change_type != change.change_type:
        raise ValueError("变更记录类型与详情不一致")
    if row.content_hash != change_content_hash(change):
        raise ValueError("变更记录内容摘要校验失败")
    return ChangeRecord(
        record_id=row.id,
        source=row.source,
        service_name=row.service_name,
        occurred_at=row.occurred_at,
        recorded_at=row.recorded_at,
        change=change,
    )
