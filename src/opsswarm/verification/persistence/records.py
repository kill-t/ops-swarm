"""恢复验证运行与不可变样本的持久化读取模型。"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Self
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from opsswarm.planning import VerificationRule
from opsswarm.planning.persistence import StoredActionStep
from opsswarm.verification.models import (
    StrictVerificationModel,
    VerificationFailure,
    VerificationSampleStatus,
    VerificationStatus,
    VerificationWorkItemError,
    VerificationWorkItemStatus,
)


class StoredVerificationSample(StrictVerificationModel):
    """一个由固定探针取得且只追加的恢复样本。"""

    id: UUID
    verification_run_id: UUID
    idempotency_key: str = Field(
        min_length=8,
        max_length=100,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]+$",
    )
    sequence: int = Field(ge=1)
    status: VerificationSampleStatus
    signal: str
    observed_value: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    threshold: float = Field(ge=0, le=1, allow_inf_nan=False)
    http_status: int | None = Field(default=None, ge=100, le=599)
    failure: VerificationFailure | None = None
    observed_at: datetime

    @field_validator("observed_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("样本时间必须包含时区")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        if self.status is VerificationSampleStatus.ERROR:
            if self.observed_value is not None or self.failure is None:
                raise ValueError("error 样本必须且只能包含失败信息")
            return self
        if self.observed_value is None or self.http_status is None or self.failure is not None:
            raise ValueError("数值样本必须且只能包含观测值和 HTTP 状态")
        passed = self.observed_value < self.threshold
        if passed != (self.status is VerificationSampleStatus.PASSED):
            raise ValueError("样本状态与阈值比较结果不一致")
        return self


class StoredVerificationRun(StrictVerificationModel):
    """与一次成功操作唯一绑定的持久恢复验证。"""

    id: UUID
    operation_execution_id: UUID
    incident_id: UUID
    action_step_id: UUID
    status: VerificationStatus
    verifier_name: str
    incident_version_before: int = Field(ge=1)
    rule: VerificationRule
    successful_sample_count: int = Field(ge=0)
    first_success_at: datetime | None = None
    last_observed_at: datetime | None = None
    failure: VerificationFailure | None = None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None
    samples: tuple[StoredVerificationSample, ...] = ()

    @field_validator(
        "first_success_at",
        "last_observed_at",
        "created_at",
        "updated_at",
        "completed_at",
    )
    @classmethod
    def normalize_time(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("验证时间必须包含时区")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_lifecycle(self) -> Self:
        if self.updated_at < self.created_at:
            raise ValueError("验证更新时间不能早于创建时间")
        if tuple(item.sequence for item in self.samples) != tuple(range(1, len(self.samples) + 1)):
            raise ValueError("验证样本序号必须从 1 开始连续递增")
        if any(item.verification_run_id != self.id for item in self.samples):
            raise ValueError("验证样本必须属于当前运行")
        if any(
            item.signal != self.rule.signal or item.threshold != self.rule.threshold
            for item in self.samples
        ):
            raise ValueError("验证样本必须使用运行冻结的规则")
        passed_samples = tuple(
            item for item in self.samples if item.status is VerificationSampleStatus.PASSED
        )
        if self.successful_sample_count != len(passed_samples):
            raise ValueError("成功样本计数与样本明细不一致")
        expected_first = passed_samples[0].observed_at if passed_samples else None
        expected_last = self.samples[-1].observed_at if self.samples else None
        if self.first_success_at != expected_first or self.last_observed_at != expected_last:
            raise ValueError("验证汇总时间与样本明细不一致")
        if self.status is VerificationStatus.RUNNING:
            if self.failure is not None or self.completed_at is not None:
                raise ValueError("running 验证不能提前包含终态")
            return self
        if self.completed_at is None or self.completed_at < self.created_at:
            raise ValueError("验证终态必须包含有效完成时间")
        if self.status is VerificationStatus.FAILED:
            if self.failure is None:
                raise ValueError("failed 验证必须包含失败信息")
            return self
        if self.failure is not None:
            raise ValueError("passed 验证不能包含失败信息")
        if self.successful_sample_count < self.rule.required_successful_samples:
            raise ValueError("passed 验证没有满足成功样本数")
        if self.first_success_at is None or self.last_observed_at is None:
            raise ValueError("passed 验证缺少观察窗口")
        if self.last_observed_at - self.first_success_at < timedelta(
            seconds=self.rule.observation_window_seconds
        ):
            raise ValueError("passed 验证没有覆盖完整观察窗口")
        return self


class StoredVerificationWorkItem(StrictVerificationModel):
    """恢复验证 Worker 可以领取和重放的持久调度状态。"""

    id: UUID
    verification_run_id: UUID
    status: VerificationWorkItemStatus
    next_sample_sequence: int = Field(ge=1)
    available_at: datetime
    locked_by: str | None = None
    locked_until: datetime | None = None
    lease_token: UUID | None = None
    attempt_count: int = Field(ge=0)
    consecutive_failure_count: int = Field(ge=0)
    last_error: VerificationWorkItemError | None = None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None

    @field_validator("available_at", "locked_until", "created_at", "updated_at", "completed_at")
    @classmethod
    def normalize_work_time(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("调度任务时间必须包含时区")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_work_lifecycle(self) -> Self:
        if self.updated_at < self.created_at or self.available_at < self.created_at:
            raise ValueError("调度任务时间顺序无效")
        has_full_lease = (
            self.locked_by is not None
            and self.locked_until is not None
            and self.lease_token is not None
        )
        has_any_lease = (
            self.locked_by is not None
            or self.locked_until is not None
            or self.lease_token is not None
        )
        if self.status is VerificationWorkItemStatus.RUNNING:
            if not has_full_lease or self.completed_at is not None:
                raise ValueError("running 调度任务必须且只能包含完整租约")
            return self
        if has_any_lease:
            raise ValueError("非 running 调度任务不能保留租约")
        if self.status is VerificationWorkItemStatus.COMPLETED:
            if self.completed_at is None:
                raise ValueError("completed 调度任务必须包含完成时间")
        elif self.completed_at is not None:
            raise ValueError("pending 调度任务不能提前完成")
        return self


@dataclass(frozen=True, slots=True)
class VerificationRunCreation:
    """区分首次创建和按执行记录幂等读取。"""

    run: StoredVerificationRun
    created: bool


@dataclass(frozen=True, slots=True)
class PreparedVerificationSample:
    """探针调用前重建的运行、步骤和可能存在的同键样本。"""

    run: StoredVerificationRun
    step: StoredActionStep
    existing_sample: StoredVerificationSample | None


@dataclass(frozen=True, slots=True)
class VerificationSampleOutcome:
    """一次采样调用产生或读取的持久结果。"""

    run: StoredVerificationRun
    sample: StoredVerificationSample
    created: bool
    probe_performed: bool


def scheduled_sample_idempotency_key(work_item_id: UUID, sample_sequence: int) -> str:
    """为一次可重领调度生成稳定且满足 API 约束的样本幂等键。"""

    if sample_sequence < 1:
        raise ValueError("sample_sequence 必须大于等于 1")
    return f"verification-scheduler-{work_item_id.hex}-{sample_sequence}"
