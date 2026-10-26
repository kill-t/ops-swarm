"""发布演练的行锁、完整命令幂等和失败关闭持久化用例。"""

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
from opsswarm.fault_lab.release.dispatch_store import LabReleaseDispatchStore
from opsswarm.fault_lab.release.policy import command_hash as command_hash
from opsswarm.fault_lab.release.policy import transition
from opsswarm.fault_lab.release.queries import read_command, read_receipt, read_state
from opsswarm.fault_lab.release.receipts import ReleaseReceipt
from opsswarm.fault_lab.release.tables import LabReleaseCommandRow, LabReleaseStateRow


def utc_now() -> datetime:
    return datetime.now(UTC)


class LabReleaseStore:
    def __init__(
        self, factory: async_sessionmaker[AsyncSession], *, clock: Callable[[], datetime] = utc_now
    ) -> None:
        self._factory, self._clock = factory, clock
        self.dispatches = LabReleaseDispatchStore(factory)

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
                await read_state(session)
        except SQLAlchemyError as error:
            raise _unavailable() from error

    async def get(self) -> ReleaseState:
        try:
            async with self._factory() as session, session.begin():
                _, state = await read_state(session)
                return state
        except SQLAlchemyError as error:
            raise _unavailable() from error

    async def change(self, command: ReleaseCommand) -> ReleaseResult:
        return await self._change(ReleaseCommand.model_validate(command.model_dump()))

    async def get_receipt(self, idempotency_key: str) -> ReleaseReceipt:
        try:
            async with self._factory() as session, session.begin():
                _, state = await read_state(session)
                return await read_receipt(session, idempotency_key, state)
        except SQLAlchemyError as error:
            raise _unavailable() from error

    async def set_legacy_fault(self, *, enabled: bool) -> ReleaseResult:
        return await self._change(
            LegacyFaultCommand(enabled=enabled, idempotency_key=f"legacy-{uuid4()}")
        )

    async def _change(self, command: ReleaseCommand | LegacyFaultCommand) -> ReleaseResult:
        try:
            async with self._factory() as session, session.begin():
                row, state = await read_state(session, lock=True)
                body = command.model_dump(mode="json")
                prior = await session.scalar(
                    select(LabReleaseCommandRow).where(
                        LabReleaseCommandRow.service_name == "order-service",
                        LabReleaseCommandRow.idempotency_key == command.idempotency_key,
                    )
                )
                if prior is not None:
                    record = read_command(prior)
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
                _, current = await read_state(session)
                return _result(True, read_command(created), current)
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
