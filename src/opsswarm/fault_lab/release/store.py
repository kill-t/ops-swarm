"""发布演练的行锁、完整命令幂等和失败关闭持久化用例。"""

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.fault_lab.release.contracts import (
    LegacyFaultCommand,
    ReleaseCommand,
    ReleaseCommandRecord,
    ReleaseError,
    ReleaseResult,
    ReleaseState,
)
from opsswarm.fault_lab.release.policy import transition
from opsswarm.fault_lab.release.tables import LabReleaseCommandRow, LabReleaseStateRow


def utc_now() -> datetime:
    return datetime.now(UTC)


def command_hash(command: object) -> str:
    return hashlib.sha256(
        json.dumps(
            command, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    ).hexdigest()


class LabReleaseStore:
    def __init__(
        self, factory: async_sessionmaker[AsyncSession], *, clock: Callable[[], datetime] = utc_now
    ) -> None:
        self._factory, self._clock = factory, clock

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("演练状态时钟必须包含时区")
        return value.astimezone(UTC)

    async def initialize(self) -> None:
        try:
            async with self._factory() as session, session.begin():
                await session.execute(
                    insert(LabReleaseStateRow)
                    .values(
                        service_name="order-service",
                        version="stable",
                        revision=1,
                        command_count=0,
                        last_command_id=None,
                        updated_at=self._now(),
                    )
                    .on_conflict_do_nothing(index_elements=["service_name"])
                )
                await _read_state(session)
        except SQLAlchemyError as error:
            raise _unavailable() from error

    async def get(self) -> ReleaseState:
        try:
            async with self._factory() as session, session.begin():
                _, state = await _read_state(session)
                return state
        except SQLAlchemyError as error:
            raise _unavailable() from error

    async def change(self, command: ReleaseCommand) -> ReleaseResult:
        return await self._change(ReleaseCommand.model_validate(command.model_dump()))

    async def set_legacy_fault(self, *, enabled: bool) -> ReleaseResult:
        return await self._change(
            LegacyFaultCommand(enabled=enabled, idempotency_key=f"legacy-{uuid4()}")
        )

    async def _change(self, command: ReleaseCommand | LegacyFaultCommand) -> ReleaseResult:
        try:
            async with self._factory() as session, session.begin():
                row, state = await _read_state(session, lock=True)
                body = command.model_dump(mode="json")
                prior = await session.scalar(
                    select(LabReleaseCommandRow).where(
                        LabReleaseCommandRow.service_name == "order-service",
                        LabReleaseCommandRow.idempotency_key == command.idempotency_key,
                    )
                )
                if prior is not None:
                    record = _read_command(prior)
                    if record.command != command or record.command_sha256 != command_hash(body):
                        raise ReleaseError(
                            "lab_release_idempotency_conflict", "幂等键已绑定另一完整演练命令"
                        )
                    return _result(False, record, state)
                version, revision = transition(state.version, state.revision, command)
                now = self._now()
                if now < state.updated_at:
                    raise ReleaseError("lab_release_clock_conflict", "演练命令时间不能早于当前状态")
                if state.command_count == 2147483647:
                    raise ReleaseError("lab_release_sequence_exhausted", "演练命令序号已达上限")
                created = LabReleaseCommandRow(
                    id=uuid4(),
                    service_name="order-service",
                    sequence=state.command_count + 1,
                    kind="cas" if isinstance(command, ReleaseCommand) else "legacy_fault_switch",
                    idempotency_key=command.idempotency_key,
                    command=body,
                    command_sha256=command_hash(body),
                    previous_version=state.version,
                    previous_revision=state.revision,
                    version=version,
                    revision=revision,
                    created_at=now,
                )
                session.add(created)
                await session.flush()
                # 数据库将命令与当前状态一次性提交；不能把旧ORM快照当作更新结果。
                await session.refresh(row)
                _, current = await _read_state(session)
                return _result(True, _read_command(created), current)
        except SQLAlchemyError as error:
            raise _unavailable() from error


def _result(applied: bool, command: ReleaseCommandRecord, state: ReleaseState) -> ReleaseResult:
    return ReleaseResult(
        applied=applied,
        command=command,
        current=state,
        effect_current=command.version == state.version and command.revision == state.revision,
    )


def _unavailable() -> ReleaseError:
    return ReleaseError("lab_release_unavailable", "持久发布演练状态不可用", 503)


def _read_command(row: LabReleaseCommandRow) -> ReleaseCommandRecord:
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


async def _read_state(
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
            record = _read_command(latest)
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
