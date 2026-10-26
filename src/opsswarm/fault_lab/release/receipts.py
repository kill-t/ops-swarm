"""只读回执的完整来源、命令摘要和历史/当前关联契约。"""

import re
from datetime import UTC, datetime
from typing import Literal, Self

from pydantic import ConfigDict, Field, field_validator, model_validator

from opsswarm.fault_lab.release.contracts import (
    Key,
    ReleaseCommandRecord,
    ReleaseModel,
    ReleaseState,
)
from opsswarm.fault_lab.release.policy import command_hash

TIME_PATTERN = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$"


def receipt_time(value: object) -> datetime:
    if isinstance(value, str):
        if re.fullmatch(TIME_PATTERN, value) is None:
            raise ValueError("回执时间须为带时区ISO8601，最多六位小数")
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("回执时间须包含时区，不能使用epoch")
    try:
        return value.astimezone(UTC)
    except OverflowError as error:
        raise ValueError("回执时间归一UTC后越界") from error


class ReleaseReceipt(ReleaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always")

    version: Literal["lab-release-receipt-v1"]
    service_name: Literal["order-service"]
    source: Literal["local-release-simulation-v1"]
    simulation: Literal[True]
    idempotency_key: Key
    status: Literal["recorded", "not_found"]
    command: ReleaseCommandRecord | None
    current: ReleaseState
    effect_current: bool = Field(strict=True)

    @field_validator("simulation", mode="before")
    @classmethod
    def require_simulation(cls, value: object) -> object:
        if value is not True:
            raise ValueError("回执必须明确标识本地模拟来源")
        return value

    @field_validator("current", mode="before")
    @classmethod
    def require_complete_state(cls, value: object) -> object:
        raw = value.model_dump() if isinstance(value, ReleaseState) else value
        if not isinstance(raw, dict) or set(raw) != set(ReleaseState.model_fields):
            raise ValueError("当前状态必须携带完整来源，不填补默认字段")
        if raw["simulation"] is not True:
            raise ValueError("当前状态必须明确标识本地模拟来源")
        return {**raw, "updated_at": receipt_time(raw["updated_at"])}

    @field_validator("command", mode="before")
    @classmethod
    def require_complete_command(cls, value: object) -> object:
        if value is None:
            return None
        raw = value.model_dump() if isinstance(value, ReleaseCommandRecord) else value
        if not isinstance(raw, dict) or set(raw) != set(ReleaseCommandRecord.model_fields):
            raise ValueError("回执必须携带完整历史命令")
        return {**raw, "created_at": receipt_time(raw["created_at"])}

    @model_validator(mode="after")
    def require_receipt_consistency(self) -> Self:
        state, record = self.current, self.command
        if state.revision > state.command_count + 1 or (state.version == "stable") != (
            state.revision % 2 == 1
        ):
            raise ValueError("当前修订超过成功命令数")
        if record is None:
            if self.status != "not_found" or self.effect_current:
                raise ValueError("缺失回执不能描述为已应用")
            return self
        if (
            self.status != "recorded"
            or record.command.idempotency_key != self.idempotency_key
            or command_hash(record.command.model_dump(mode="json")) != record.command_sha256
            or record.previous_revision > record.sequence
            or (record.previous_version == "stable") != (record.previous_revision % 2 == 1)
            or (record.sequence == 1 and record.previous_revision != 1)
            or record.sequence > state.command_count
            or record.revision > state.revision
            or state.revision - record.revision > state.command_count - record.sequence
            or record.created_at > state.updated_at
        ):
            raise ValueError("回执命令、摘要或当前状态关联不一致")
        current = record.version == state.version and record.revision == state.revision
        if self.effect_current is not current:
            raise ValueError("历史效果适用性必须由版本和修订共同决定")
        if record.sequence == state.command_count and (
            not current or record.created_at != state.updated_at
        ):
            raise ValueError("最新命令必须精确对应当前状态")
        return self


def make_receipt(
    key: str, record: ReleaseCommandRecord | None, state: ReleaseState
) -> ReleaseReceipt:
    return ReleaseReceipt(
        version="lab-release-receipt-v1",
        service_name="order-service",
        source="local-release-simulation-v1",
        simulation=True,
        idempotency_key=key,
        status="not_found" if record is None else "recorded",
        command=record,
        current=state,
        effect_current=record is not None
        and record.version == state.version
        and record.revision == state.revision,
    )
