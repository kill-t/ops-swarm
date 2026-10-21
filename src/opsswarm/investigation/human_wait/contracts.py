"""人工陈述、等待版本及命令结果的严格有界契约。"""

from datetime import UTC, datetime
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, ConfigDict, Field, field_validator, model_validator

from opsswarm.investigation.models import StrictModel

MAX_SUPPLEMENTS = 15
MAX_WAIT_EVENTS = MAX_SUPPLEMENTS + 1
MAX_COMMAND_BYTES = 65536
Hash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class HumanWaitModel(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class HumanInputItem(HumanWaitModel):
    """分类记录操作者陈述，不赋予工具证据身份。"""

    kind: Literal["statement", "constraint", "question"]
    text: str = Field(min_length=1, max_length=1000)
    observed_at: AwareDatetime | None = None

    @field_validator("observed_at")
    @classmethod
    def normalize_observed_at(cls, value: datetime | None) -> datetime | None:
        return value.astimezone(UTC) if value is not None else None

    @model_validator(mode="after")
    def validate_observation(self) -> Self:
        if self.kind != "statement" and self.observed_at is not None:
            raise ValueError("只有人工陈述可以附带声明的观察时间")
        return self


class WaitCommandBase(HumanWaitModel):
    handoff_id: UUID
    source_sha256: Hash
    expected_wait_version: int = Field(ge=1, le=MAX_WAIT_EVENTS + 1, strict=True)
    idempotency_key: str = Field(
        min_length=8, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]+$"
    )
    reason: str = Field(min_length=1, max_length=1000)


class SupplementWaitCommand(WaitCommandBase):
    action: Literal["supplement"] = "supplement"
    items: tuple[HumanInputItem, ...] = Field(min_length=1, max_length=5)


class CancelWaitCommand(WaitCommandBase):
    action: Literal["cancel"] = "cancel"


WaitCommand = Annotated[SupplementWaitCommand | CancelWaitCommand, Field(discriminator="action")]


class HumanWaitEventRecord(HumanWaitModel):
    id: UUID
    investigation_run_id: UUID
    handoff_id: UUID
    state_version: int = Field(ge=2, le=MAX_WAIT_EVENTS + 1, strict=True)
    action: Literal["supplement", "cancel"]
    subject_id: str = Field(min_length=1, max_length=100)
    authorization_id: UUID
    command: WaitCommand
    command_sha256: Hash
    created_at: AwareDatetime


class HumanWaitState(HumanWaitModel):
    investigation_run_id: UUID
    handoff_id: UUID
    source_sha256: Hash
    state_version: int = Field(ge=1, le=MAX_WAIT_EVENTS + 1, strict=True)
    status: Literal["waiting", "cancelled"]
    events: tuple[HumanWaitEventRecord, ...] = Field(max_length=MAX_WAIT_EVENTS)


class HumanWaitResult(HumanWaitModel):
    applied: bool
    event: HumanWaitEventRecord


class HumanWaitAuditRecord(HumanWaitModel):
    cursor: int = Field(ge=1)
    authorization_id: UUID
    investigation_run_id: UUID
    subject_id: str
    command: WaitCommand
    command_sha256: Hash
    outcome: Literal["applied", "replayed", "error"]
    http_status: Literal[200, 201, 404, 409]
    event_id: UUID | None
    error_code: str | None
    error_message: str | None
    created_at: AwareDatetime


class HumanWaitAuditPage(HumanWaitModel):
    items: tuple[HumanWaitAuditRecord, ...] = Field(max_length=100)
    next_cursor: int | None


class HumanWaitError(ValueError):
    def __init__(self, code: str, message: str, status_code: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
