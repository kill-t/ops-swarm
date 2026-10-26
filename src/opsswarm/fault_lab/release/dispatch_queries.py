"""已锁定发布头下的远端操作完整重建与数据库时钟投影。"""

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from opsswarm.fault_lab.release.contracts import ReleaseError, ReleaseState
from opsswarm.fault_lab.release.dispatch_contracts import DispatchRecord, DispatchView
from opsswarm.fault_lab.release.dispatch_tables import LabReleaseDispatchRow
from opsswarm.fault_lab.release.policy import command_hash
from opsswarm.fault_lab.release.queries import read_receipt
from opsswarm.fault_lab.release.receipts import receipt_time


async def database_now(session: AsyncSession) -> datetime:
    return receipt_time(await session.scalar(select(func.clock_timestamp())))


async def dispatch_view(
    session: AsyncSession, row: LabReleaseDispatchRow, state: ReleaseState
) -> DispatchView:
    try:
        record = DispatchRecord.model_validate(row, from_attributes=True)
        if record.command_sha256 != command_hash(row.command):
            raise ValueError("实际JSON摘要不匹配")
        receipt = await read_receipt(session, record.command.idempotency_key, state)
        command = receipt.command
        if record.status == "applied":
            if (
                command is None
                or command.id != record.command_id
                or command.kind != "cas"
                or command.command != record.command
                or command.command_sha256 != record.command_sha256
                or command.created_at != record.finished_at
            ):
                raise ValueError("派发缺少精确同源成功命令")
        elif command is not None:
            raise ValueError("未应用派发不能已存在成功命令")
        now = await database_now(session)
        if now < record.created_at:
            raise ValueError("数据库时钟早于持久操作")
        availability = (
            record.status
            if record.status != "prepared"
            else ("expired" if now >= record.expires_at else "ready")
        )
        return DispatchView(
            record=record, availability=availability, checked_at=now, receipt=receipt
        )
    except ValueError as error:
        if isinstance(error, ReleaseError):
            raise
        raise ReleaseError(
            "lab_dispatch_integrity_violation", "派发记录无法完整重建", 503
        ) from error
