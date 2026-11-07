"""无网络与持久化依赖的候选恢复判定规则。"""

from datetime import datetime

from pydantic import TypeAdapter

from opsswarm.verification.candidate.contracts import (
    POLICY_VERSION,
    ProbeObservation,
    RecoveryError,
    RecoveryOutcome,
    RecoverySampleRecord,
    UtcTime,
)

MAX_SAMPLE_AGE_SECONDS = 10
MAX_REQUEST_SECONDS = 10
MIN_SAMPLE_INTERVAL_SECONDS = 2
RECOVERY_LEASE_SECONDS = 30
_UTC_TIME = TypeAdapter(UtcTime)


def validate_observation(observation: ProbeObservation, now: datetime) -> ProbeObservation:
    """重新校验端口对象，防止 model_construct/copy 绕过严格契约。"""

    if type(observation) is not ProbeObservation:
        raise RecoveryError("invalid_observation", "探针必须返回固定观测契约")
    checked = ProbeObservation.model_validate(observation.model_dump())
    checked_now = _UTC_TIME.validate_python(now)
    if (
        checked.observed_at > checked_now
        or (checked_now - checked.observed_at).total_seconds() > MAX_SAMPLE_AGE_SECONDS
        or (checked.observed_at - checked.requested_at).total_seconds() > MAX_REQUEST_SECONDS
    ):
        raise RecoveryError("invalid_observation_time", "探针观测不在有限时效内")
    return checked


def classify(
    execution_outcome: str,
    samples: tuple[RecoverySampleRecord, ...],
    *,
    exhausted: bool = False,
) -> tuple[RecoveryOutcome, str]:
    """只有同一尝试的三份连续样本才能形成客观恢复结论。"""

    if execution_outcome == "unknown":
        return "execution_unknown", "execution_unknown"
    if execution_outcome in {"blocked", "not_applied"}:
        return "not_executed", "execution_not_applied"
    if execution_outcome == "applied_superseded":
        return "not_recovered", "execution_superseded"
    if execution_outcome != "applied_current":
        raise RecoveryError("invalid_execution_outcome", "执行结果不在固定策略范围内")
    if exhausted:
        return "source_failed", "recovery_exhausted"
    checked = tuple(RecoverySampleRecord.model_validate(item.model_dump()) for item in samples)
    if any(item.error_code is not None for item in checked):
        return "source_failed", "probe_source_failed"
    if (
        len(checked) != 3
        or tuple(item.sequence for item in checked) != (1, 2, 3)
        or len({(item.job_id, item.attempt_id) for item in checked}) != 1
    ):
        return "source_failed", "incomplete_samples"
    observations = tuple(item.observation for item in checked if item.observation is not None)
    if any(
        (right.requested_at - left.requested_at).total_seconds() < MIN_SAMPLE_INTERVAL_SECONDS
        for left, right in zip(observations, observations[1:], strict=False)
    ):
        return "source_failed", "invalid_sample_interval"
    if any(item.value == 1.0 for item in observations):
        return "not_recovered", "business_probe_failed"
    return "recovered", "business_probe_passed"


__all__ = ["POLICY_VERSION", "classify", "validate_observation"]
