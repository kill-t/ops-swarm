"""远端注册、实际CAS和取消的固定锁序与原子用例。"""

from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.fault_lab.release.contracts import ReleaseError
from opsswarm.fault_lab.release.dispatch_contracts import (
    DISPATCH_POLICY,
    DispatchCommand,
    DispatchResult,
    DispatchView,
)
from opsswarm.fault_lab.release.dispatch_queries import database_now, dispatch_view
from opsswarm.fault_lab.release.dispatch_tables import LabReleaseDispatchRow
from opsswarm.fault_lab.release.policy import command_hash, transition
from opsswarm.fault_lab.release.queries import read_state
from opsswarm.fault_lab.release.tables import LabReleaseCommandRow


class LabReleaseDispatchStore:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

    async def register(self, command: DispatchCommand) -> DispatchResult:
        checked = DispatchCommand.model_validate(command.model_dump())
        try:
            async with self._factory() as session, session.begin():
                _, state = await read_state(session, lock=True)
                row = await session.get(
                    LabReleaseDispatchRow, checked.operation_id, with_for_update=True
                )
                applied = row is None
                if row is not None:
                    view = await dispatch_view(session, row, state)
                    original = DispatchCommand.model_validate(
                        view.record.model_dump(include=set(DispatchCommand.model_fields))
                    )
                    if original != checked:
                        raise ReleaseError(
                            "lab_dispatch_idempotency_conflict", "操作ID已绑定另一原命令或窗口"
                        )
                else:
                    now = await database_now(session)
                    if not checked.not_before <= now < checked.expires_at:
                        raise ReleaseError("lab_dispatch_expired", "注册不在固定派发窗口内")
                    row = LabReleaseDispatchRow(
                        **checked.model_dump(exclude={"command"}),
                        command=checked.command.model_dump(mode="json"),
                        command_sha256=command_hash(checked.command.model_dump(mode="json")),
                        policy_version=DISPATCH_POLICY,
                        status="prepared",
                        command_id=None,
                        created_at=now,
                        finished_at=None,
                    )
                    session.add(row)
                    await session.flush()
                    view = await dispatch_view(session, row, state)
                return DispatchResult(applied=applied, dispatch=view)
        except SQLAlchemyError as error:
            raise _unavailable() from error

    async def get(self, operation_id: UUID) -> DispatchView:
        try:
            async with self._factory() as session, session.begin():
                _, state = await read_state(session)
                row = await _get_row(session, operation_id)
                return await dispatch_view(session, row, state)
        except SQLAlchemyError as error:
            raise _unavailable() from error

    async def execute(self, operation_id: UUID) -> DispatchResult:
        return await self._finish(operation_id, cancel=False)

    async def cancel(self, operation_id: UUID) -> DispatchResult:
        return await self._finish(operation_id, cancel=True)

    async def _finish(self, operation_id: UUID, *, cancel: bool) -> DispatchResult:
        try:
            async with self._factory() as session, session.begin():
                head, state = await read_state(session, lock=True)
                row = await _get_row(session, operation_id)
                view = await dispatch_view(session, row, state)
                if cancel and row.status == "applied":
                    raise ReleaseError(
                        "lab_dispatch_already_applied", "操作已应用，取消不能撤销副作用"
                    )
                if (cancel and row.status == "cancelled") or (
                    not cancel and row.status == "applied"
                ):
                    return DispatchResult(applied=False, dispatch=view)
                if not cancel and row.status == "cancelled":
                    raise ReleaseError("lab_dispatch_cancelled", "操作已取消")
                now = await database_now(session)
                if cancel:
                    row.status, row.finished_at = "cancelled", now
                else:
                    if not row.not_before <= now < row.expires_at:
                        raise ReleaseError("lab_dispatch_expired", "已超过固定派发窗口")
                    version, revision = transition(
                        state.version, state.revision, view.record.command
                    )
                    if now < state.updated_at or state.command_count == 2147483647:
                        raise ReleaseError(
                            "lab_dispatch_state_conflict", "演练时钟或命令序号不可推进"
                        )
                    session.add(
                        LabReleaseCommandRow(
                            id=uuid4(),
                            service_name="order-service",
                            sequence=state.command_count + 1,
                            kind="cas",
                            idempotency_key=view.record.command.idempotency_key,
                            command=view.record.command.model_dump(mode="json"),
                            command_sha256=row.command_sha256,
                            previous_version=state.version,
                            previous_revision=state.revision,
                            version=version,
                            revision=revision,
                            created_at=now,
                        )
                    )
                await session.flush()
                await session.refresh(row)
                # 原生命令触发器推进了发布头，不能复用会话中的旧ORM快照。
                await session.refresh(head)
                _, current = await read_state(session)
                return DispatchResult(
                    applied=True, dispatch=await dispatch_view(session, row, current)
                )
        except SQLAlchemyError as error:
            raise _unavailable() from error


async def _get_row(session: AsyncSession, operation_id: UUID) -> LabReleaseDispatchRow:
    row = await session.scalar(
        select(LabReleaseDispatchRow).where(LabReleaseDispatchRow.operation_id == operation_id)
    )
    if row is None:
        raise ReleaseError("lab_dispatch_not_found", "远端操作不存在", 404)
    return row


def _unavailable() -> ReleaseError:
    return ReleaseError("lab_dispatch_unavailable", "持久派发存储不可用或提交被拒绝", 503)
