"""在已锁定的发布头下有界重建目标命令与相邻历史。"""

from pydantic import TypeAdapter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from opsswarm.fault_lab.release.contracts import (
    Key,
    ReleaseCommandRecord,
    ReleaseError,
    ReleaseState,
)
from opsswarm.fault_lab.release.policy import command_hash
from opsswarm.fault_lab.release.receipts import ReleaseReceipt, make_receipt
from opsswarm.fault_lab.release.tables import LabReleaseCommandRow, LabReleaseStateRow


async def read_receipt(session: AsyncSession, key: str, state: ReleaseState) -> ReleaseReceipt:
    key = TypeAdapter(Key).validate_python(key)
    row = await session.scalar(
        select(LabReleaseCommandRow).where(
            LabReleaseCommandRow.service_name == "order-service",
            LabReleaseCommandRow.idempotency_key == key,
        )
    )
    try:
        if row is None:
            return make_receipt(key, None, state)
        record = read_command(row)
        neighbors = (
            await session.scalars(
                select(LabReleaseCommandRow).where(
                    LabReleaseCommandRow.service_name == "order-service",
                    LabReleaseCommandRow.sequence.in_((record.sequence - 1, record.sequence + 1)),
                )
            )
        ).all()
        adjacent = {item.sequence: read_command(item) for item in neighbors}
        if record.sequence == 1:
            if record.previous_version != "stable" or record.previous_revision != 1:
                raise ValueError("首条命令必须衔接固定初态")
        else:
            _require_link(adjacent.get(record.sequence - 1), record)
        if record.sequence < state.command_count:
            _require_link(record, adjacent.get(record.sequence + 1))
        return make_receipt(key, record, state)
    except ValueError as error:
        if isinstance(error, ReleaseError):
            raise
        raise ReleaseError(
            "lab_release_integrity_violation", "演练回执无法完整重建", 503
        ) from error


def _require_link(before: ReleaseCommandRecord | None, after: ReleaseCommandRecord | None) -> None:
    if (
        before is None
        or after is None
        or (
            before.sequence + 1 != after.sequence
            or before.version != after.previous_version
            or before.revision != after.previous_revision
            or before.created_at > after.created_at
        )
    ):
        raise ValueError("命令必须与相邻历史连续衔接")


def read_command(row: LabReleaseCommandRow) -> ReleaseCommandRecord:
    try:
        record = ReleaseCommandRecord.model_validate(row, from_attributes=True)
        if (
            row.command != record.command.model_dump(mode="json")
            or row.idempotency_key != record.command.idempotency_key
            or row.command_sha256 != command_hash(row.command)
        ):
            raise ValueError("演练命令正文或摘要不一致")
        return record
    except ValueError as error:
        raise ReleaseError(
            "lab_release_integrity_violation", "演练命令无法完整重建", 503
        ) from error


async def read_state(
    session: AsyncSession, *, lock: bool = False
) -> tuple[LabReleaseStateRow, ReleaseState]:
    statement = select(LabReleaseStateRow).where(LabReleaseStateRow.service_name == "order-service")
    # 两次读取必须处于同一状态版本；共享锁防止头行和最新命令被读成不同提交。
    statement = statement.with_for_update(read=not lock)
    row = await session.scalar(statement)
    if row is None:
        raise ReleaseError("lab_release_uninitialized", "持久发布演练状态尚未初始化", 503)
    try:
        state = ReleaseState.model_validate(
            dict(
                service_name=row.service_name,
                version=row.version,
                revision=row.revision,
                command_count=row.command_count,
                release_500_enabled=row.version == "2026.01.01-rc1",
                updated_at=row.updated_at,
            )
        )
        latest = await session.scalar(
            select(LabReleaseCommandRow)
            .where(LabReleaseCommandRow.service_name == row.service_name)
            .order_by(LabReleaseCommandRow.sequence.desc())
            .limit(1)
        )
        if row.command_count == 0:
            if (
                latest is not None
                or row.last_command_id is not None
                or row.version != "stable"
                or row.revision != 1
            ):
                raise ValueError("演练初始状态不一致")
        else:
            if latest is None:
                raise ValueError("当前状态缺少最新命令")
            record = read_command(latest)
            if (
                row.last_command_id != record.id
                or row.command_count != record.sequence
                or row.version != record.version
                or row.revision != record.revision
                or row.updated_at != record.created_at
            ):
                raise ValueError("当前状态与最新命令不一致")
        return row, state
    except ValueError as error:
        if isinstance(error, ReleaseError):
            raise
        raise ReleaseError(
            "lab_release_integrity_violation", "演练状态无法完整重建", 503
        ) from error
