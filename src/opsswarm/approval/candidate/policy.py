"""审批阶段的确定性期限与有效性，独立于草案创建阶段。"""

from datetime import UTC, datetime, timedelta

from opsswarm.approval.candidate.contracts import RequestAvailability, RequestError, RequestRecord
from opsswarm.investigation.candidate.contracts import SourceKind

REQUEST_TTL_SECONDS = 30 * 60


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("审批阶段时间必须包含时区")
    return value.astimezone(UTC)


def request_expiry(*, started_at: datetime, max_duration_seconds: int, now: datetime) -> datetime:
    """审批请求不能延长原运行截止，已截止时禁止创建零时长请求。"""

    created_at = _utc(now)
    deadline = _utc(started_at) + timedelta(seconds=max_duration_seconds)
    if created_at >= deadline:
        raise RequestError("candidate_approval_deadline_exceeded", "原运行已超过截止时间")
    return min(created_at + timedelta(seconds=REQUEST_TTL_SECONDS), deadline)


def request_availability(
    record: RequestRecord,
    *,
    source_kind: SourceKind,
    run_status: str,
    latest_wait_version: int | None,
    wait_cancelled: bool,
    incident_status: str | None,
    incident_version: int | None,
    now: datetime,
    run_deadline: datetime,
) -> RequestAvailability:
    """仅对已重建完整事实的请求计算阶段，不复用草案的 current。"""

    if _utc(now) >= min(_utc(record.expires_at), _utc(run_deadline)):
        return "expired"
    source_current = (
        source_kind == "terminal_handoff"
        and run_status == "await_candidate"
        and record.wait_version is None
    ) or (
        source_kind == "human_route"
        and run_status == "await_human"
        and not wait_cancelled
        and record.wait_version is not None
        and latest_wait_version == record.wait_version
    )
    if (
        not source_current
        or incident_status != "awaiting_approval"
        or incident_version != record.incident_version
    ):
        return "stale"
    return "current"
