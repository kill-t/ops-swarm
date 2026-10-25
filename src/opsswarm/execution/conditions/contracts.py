"""条件目录、可信来源声明与不可变完整核验快照的严格契约。"""

import json
import re
from datetime import UTC, datetime
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator, model_validator

from opsswarm.investigation.candidate.output import CandidateItem, EvidenceId
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.human_wait.contracts import Hash, HumanWaitModel

POLICY_VERSION: Literal["candidate-condition-verification-v1"] = (
    "candidate-condition-verification-v1"
)
MAX_VERSIONS = 8
MAX_COMMAND_BYTES = 24 * 1024
OBSERVATION_TIME_PATTERN = (
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]"
    r"(?:\.[0-9]{1,6})?(?:Z|[+-](?:0[0-9]|1[0-5]):[0-5][0-9])$"
)
ConditionKind = Literal["precondition", "question"]
ConditionStatus = Literal["verified", "unresolved", "not_applicable"]
ConditionAvailability = Literal["pending", "current", "superseded", "stale", "expired"]


class ConditionItem(HumanWaitModel):
    condition_id: Hash
    kind: ConditionKind
    position: int = Field(ge=1, le=10, strict=True)
    text: CandidateItem
    text_sha256: Hash


class EvidenceReference(HumanWaitModel):
    kind: Literal["evidence"] = "evidence"
    evidence_id: EvidenceId


class HumanObservation(HumanWaitModel):
    """人工声明拥有独立来源身份，不转化为工具采集证据。"""

    kind: Literal["human_observation"] = "human_observation"
    text: str = Field(min_length=1, max_length=500)
    observed_at: AwareDatetime

    @field_validator("observed_at", mode="before")
    @classmethod
    def require_timezone_value(cls, value: object) -> object:
        if not isinstance(value, (datetime, str)):
            raise ValueError("人工观察时间必须是带时区的时间或ISO8601字符串")
        if isinstance(value, str):
            if re.fullmatch(OBSERVATION_TIME_PATTERN, value) is None:
                raise ValueError("人工观察时间须使用带时区ISO8601格式，精度最多六位小数")
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError("人工观察时间必须包含时区")
        return value

    @field_validator("observed_at")
    @classmethod
    def normalize_observed_at(cls, value: datetime) -> datetime:
        try:
            return value.astimezone(UTC)
        except OverflowError as error:
            raise ValueError("人工观察时间归一UTC后必须处于有效日期范围") from error


class PreflightReference(HumanWaitModel):
    kind: Literal["preflight"] = "preflight"
    preflight_id: UUID
    content_sha256: Hash


ConditionReference = Annotated[
    EvidenceReference | HumanObservation | PreflightReference, Field(discriminator="kind")
]


class ConditionInput(HumanWaitModel):
    condition_id: Hash
    status: ConditionStatus
    explanation: str = Field(min_length=1, max_length=500)
    sources: tuple[ConditionReference, ...] = Field(max_length=3)

    @model_validator(mode="after")
    def validate_sources(self) -> Self:
        _validate_sources(self.status, self.sources)
        return self


def _validate_sources(status: ConditionStatus, sources: tuple[ConditionReference, ...]) -> None:
    if status != "unresolved" and not sources:
        raise ValueError("非未解决条件必须保留至少一个来源")
    if len({content_hash(source) for source in sources}) != len(sources):
        raise ValueError("同一条件的来源不能重复")


class ConditionCommand(HumanWaitModel):
    decision_id: UUID
    decision_content_sha256: Hash
    expected_incident_version: int = Field(ge=2, strict=True)
    expected_condition_version: int = Field(ge=0, le=MAX_VERSIONS - 1, strict=True)
    previous_content_sha256: Hash | None
    items: tuple[ConditionInput, ...] = Field(min_length=1, max_length=20)
    idempotency_key: str = Field(
        min_length=8, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]+$"
    )
    reason: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_snapshot(self) -> Self:
        if (self.expected_condition_version == 0) != (self.previous_content_sha256 is None):
            raise ValueError("只有首次核验允许空前驱摘要")
        if len({item.condition_id for item in self.items}) != len(self.items):
            raise ValueError("核验条目不能重复")
        preflights = {
            source.preflight_id
            for item in self.items
            for source in item.sources
            if isinstance(source, PreflightReference)
        }
        if len(preflights) > 3:
            raise ValueError("一份核验最多引用三个不同预检")
        canonical = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if len(canonical) > MAX_COMMAND_BYTES:
            raise ValueError("规范核验命令不能超过24KiB")
        return self


class ConditionResultItem(HumanWaitModel):
    condition: ConditionItem
    status: ConditionStatus
    explanation: str = Field(min_length=1, max_length=500)
    sources: tuple[ConditionReference, ...] = Field(max_length=3)

    @model_validator(mode="after")
    def validate_sources(self) -> Self:
        _validate_sources(self.status, self.sources)
        return self


class _ConditionFlagsModel(HumanWaitModel):
    """固定授权标志拒绝数值布尔，避免JSON形状被宽松字面量校验掩盖。"""

    @field_validator("approval_required", "execution_eligible", mode="before", check_fields=False)
    @classmethod
    def require_boolean_flags(cls, value: object) -> object:
        if type(value) is not bool:
            raise ValueError("审批与执行标志必须为JSON布尔值")
        return value


class ConditionContent(_ConditionFlagsModel):
    version: Literal["candidate-condition-verification-content-v1"] = (
        "candidate-condition-verification-content-v1"
    )
    decision_id: UUID
    decision_content_sha256: Hash
    plan_id: UUID
    plan_content_sha256: Hash
    items: tuple[ConditionResultItem, ...] = Field(min_length=1, max_length=20)
    status: Literal["blocked", "ready_for_review", "plan_revision_required"]
    approval_required: Literal[True] = True
    execution_eligible: Literal[False] = False


class ConditionRecord(HumanWaitModel):
    id: UUID
    investigation_run_id: UUID
    candidate_job_id: UUID
    decision_id: UUID
    decision_content_sha256: Hash
    version: int = Field(ge=1, le=MAX_VERSIONS, strict=True)
    previous_id: UUID | None
    previous_content_sha256: Hash | None
    incident_version: int = Field(ge=2, strict=True)
    policy_version: Literal["candidate-condition-verification-v1"]
    verified_by_id: str = Field(min_length=1, max_length=100)
    authorization_id: UUID
    idempotency_key: str = Field(min_length=8, max_length=100)
    command_sha256: Hash
    content: ConditionContent
    content_sha256: Hash
    created_at: AwareDatetime
    expires_at: AwareDatetime


class ConditionState(_ConditionFlagsModel):
    catalog: tuple[ConditionItem, ...] = Field(min_length=1, max_length=20)
    condition_version: int = Field(ge=0, le=MAX_VERSIONS, strict=True)
    latest: ConditionRecord | None
    availability: ConditionAvailability
    approval_required: Literal[True] = True
    execution_eligible: Literal[False] = False


class ConditionVersionView(HumanWaitModel):
    record: ConditionRecord
    availability: ConditionAvailability


class ConditionResult(HumanWaitModel):
    applied: bool
    verification: ConditionVersionView


class ConditionAuditRecord(HumanWaitModel):
    cursor: int
    authorization_id: UUID
    investigation_run_id: UUID
    candidate_job_id: UUID
    subject_id: str
    command: ConditionCommand
    command_sha256: Hash
    outcome: Literal["applied", "replayed", "error"]
    http_status: Literal[200, 201, 404, 409]
    verification_id: UUID | None
    error_code: str | None
    error_message: str | None
    created_at: AwareDatetime


class ConditionAuditPage(HumanWaitModel):
    items: tuple[ConditionAuditRecord, ...] = Field(max_length=100)
    next_cursor: int | None


class ConditionError(ValueError):
    def __init__(self, code: str, message: str, status_code: int = 409) -> None:
        super().__init__(message)
        self.code, self.status_code = code, status_code
