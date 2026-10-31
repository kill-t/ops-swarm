"""审批请求自己的有效阶段与原运行截止，不沿用旧草案current。"""

from datetime import datetime

from opsswarm.approval.candidate.contracts import RequestError
from opsswarm.approval.candidate.policy import request_expiry as candidate_expiry
from opsswarm.approval.reinvestigation_candidate.contracts import (
    ReflowRequestError,
    ReflowRequestRecord,
    RequestAvailability,
)


def request_expiry(*, started_at: datetime, max_duration_seconds: int, now: datetime) -> datetime:
    try:
        return candidate_expiry(
            started_at=started_at, max_duration_seconds=max_duration_seconds, now=now
        )
    except RequestError as error:
        raise ReflowRequestError(
            "reflow_approval_deadline_exceeded", "原运行已超过截止时间"
        ) from error


def request_availability(
    record: ReflowRequestRecord,
    *,
    basis_current: bool,
    now: datetime,
    run_deadline: datetime,
) -> RequestAvailability:
    if any(value.tzinfo is None or value.utcoffset() is None for value in (now, run_deadline)):
        raise ValueError("审批阶段时间必须包含时区")
    if now >= min(record.expires_at, run_deadline):
        return "expired"
    return "current" if basis_current else "stale"
