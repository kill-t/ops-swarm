"""远端固定窗口操作的输入、持久事实与查询投影。"""

from datetime import datetime, timedelta
from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, field_validator, model_validator

from opsswarm.fault_lab.release.contracts import ReleaseCommand, ReleaseModel
from opsswarm.fault_lab.release.policy import command_hash
from opsswarm.fault_lab.release.receipts import ReleaseReceipt, receipt_time

DISPATCH_POLICY = "lab-release-dispatch-v1"


class DispatchCommand(ReleaseModel):
    operation_id: UUID
    command: ReleaseCommand
    not_before: AwareDatetime
    expires_at: AwareDatetime

    @field_validator("not_before", "expires_at", mode="before")
    @classmethod
    def require_time(cls, value: object) -> datetime:
        return receipt_time(value)

    @field_validator("command", mode="before")
    @classmethod
    def recheck_command(cls, value: object) -> object:
        return value.model_dump() if isinstance(value, ReleaseCommand) else value

    @model_validator(mode="after")
    def require_bounded_rollback(self) -> Self:
        if (
            self.command.expected_version != "2026.01.01-rc1"
            or self.command.target_version != "stable"
            or self.command.idempotency_key != f"candidate-release-{self.operation_id}"
            or not timedelta(0) < self.expires_at - self.not_before <= timedelta(seconds=30)
        ):
            raise ValueError("派发须固定原操作键、rc1到stable与至多30秒窗口")
        return self


class DispatchRecord(DispatchCommand):
    policy_version: Literal["lab-release-dispatch-v1"]
    command_sha256: str
    status: Literal["prepared", "applied", "cancelled"]
    command_id: UUID | None
    created_at: AwareDatetime
    finished_at: AwareDatetime | None

    @field_validator("created_at", "finished_at", mode="before")
    @classmethod
    def require_record_time(cls, value: object) -> datetime | None:
        return None if value is None else receipt_time(value)

    @model_validator(mode="after")
    def require_consistent_record(self) -> Self:
        if (
            self.command_sha256 != command_hash(self.command.model_dump(mode="json"))
            or not self.not_before <= self.created_at < self.expires_at
            or (self.status == "prepared") != (self.finished_at is None)
            or (self.status == "applied") != (self.command_id is not None)
            or (self.finished_at is not None and self.finished_at < self.created_at)
            or (
                self.status == "applied"
                and self.finished_at is not None
                and self.finished_at >= self.expires_at
            )
        ):
            raise ValueError("派发记录缺少精确原文、窗口或终态引用")
        return self


class DispatchView(ReleaseModel):
    record: DispatchRecord
    availability: Literal["ready", "expired", "applied", "cancelled"]
    checked_at: AwareDatetime
    receipt: ReleaseReceipt


class DispatchResult(ReleaseModel):
    applied: bool
    dispatch: DispatchView
