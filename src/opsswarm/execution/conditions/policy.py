"""原文目录身份、完整快照、连续版本与当前适用性的纯业务规则。"""

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Literal
from uuid import UUID

from opsswarm.approval.decision.contracts import DecisionAvailability, DecisionRecord
from opsswarm.execution.conditions.contracts import (
    MAX_VERSIONS,
    ConditionAvailability,
    ConditionCommand,
    ConditionContent,
    ConditionError,
    ConditionInput,
    ConditionItem,
    ConditionKind,
    ConditionRecord,
    ConditionResultItem,
    HumanObservation,
)
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.human_wait.contracts import Hash, HumanWaitModel
from opsswarm.planning.candidate.contracts import PlanRecord


class _ConditionIdentity(HumanWaitModel):
    plan_id: UUID
    plan_content_sha256: Hash
    kind: ConditionKind
    position: int
    text_sha256: Hash


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("核验时间必须包含时区")
    return value.astimezone(UTC)


def build_catalog(plan: PlanRecord) -> tuple[ConditionItem, ...]:
    """身份绑定计划和类别序位，跨类别相同文案仍是独立条件。"""

    groups: tuple[tuple[ConditionKind, tuple[str, ...]], ...] = (
        ("precondition", plan.content.preconditions),
        ("question", plan.content.unresolved_questions),
    )
    items = []
    for kind, texts in groups:
        for position, text in enumerate(texts, start=1):
            text_sha256 = sha256(text.encode("utf-8")).hexdigest()
            identity = _ConditionIdentity(
                plan_id=plan.id,
                plan_content_sha256=plan.content_sha256,
                kind=kind,
                position=position,
                text_sha256=text_sha256,
            )
            items.append(
                ConditionItem(
                    condition_id=content_hash(identity),
                    kind=kind,
                    position=position,
                    text=text,
                    text_sha256=text_sha256,
                )
            )
    return tuple(items)


def build_content(
    decision: DecisionRecord, plan: PlanRecord, command: ConditionCommand
) -> ConditionContent:
    """只绑定已批准原计划并复制完整原文，来源真实性留给应用层核验。"""

    if (
        decision.outcome != "approved"
        or decision.content.outcome != "approved"
        or decision.content_sha256 != content_hash(decision.content)
        or plan.content_sha256 != content_hash(plan.content)
        or decision.id != command.decision_id
        or decision.content_sha256 != command.decision_content_sha256
        or command.expected_incident_version != decision.incident_version
        or decision.content.plan_id != plan.id
        or decision.plan_content_sha256 != plan.content_sha256
        or decision.content.plan_content_sha256 != plan.content_sha256
        or decision.investigation_run_id != plan.investigation_run_id
        or decision.candidate_job_id != plan.candidate_job_id
        or decision.content.candidate_job_id != plan.candidate_job_id
        or plan.content.candidate_job_id != plan.candidate_job_id
        or decision.content.preconditions != plan.content.preconditions
        or decision.content.unresolved_questions != plan.content.unresolved_questions
        or decision.content.evidence_ids != plan.content.evidence_ids
        or decision.content.wait_version != plan.wait_version
    ):
        raise ConditionError("candidate_condition_source_invalid", "核验未精确绑定已批准的原计划")
    catalog = build_catalog(plan)
    if tuple(item.condition_id for item in command.items) != tuple(
        item.condition_id for item in catalog
    ):
        raise ConditionError(
            "candidate_condition_catalog_mismatch", "核验必须按目录顺序完整覆盖原条件"
        )
    results = tuple(
        ConditionResultItem(
            condition=condition,
            status=item.status,
            explanation=item.explanation,
            sources=item.sources,
        )
        for condition, item in zip(catalog, command.items, strict=True)
    )
    statuses = {item.status for item in results}
    status: Literal["blocked", "ready_for_review", "plan_revision_required"] = (
        "blocked"
        if "unresolved" in statuses
        else "plan_revision_required"
        if "not_applicable" in statuses
        else "ready_for_review"
    )
    return ConditionContent(
        decision_id=decision.id,
        decision_content_sha256=decision.content_sha256,
        plan_id=plan.id,
        plan_content_sha256=plan.content_sha256,
        items=results,
        status=status,
    )


def validate_version(command: ConditionCommand, latest: ConditionRecord | None) -> None:
    """只允许在精确前驱之后追加，过期不会重置版本或清空历史。"""

    if latest is not None and latest.version >= MAX_VERSIONS:
        raise ConditionError("candidate_condition_version_limit", "每个决定最多保存八个核验版本")
    if latest is None:
        matches = (
            command.expected_condition_version == 0 and command.previous_content_sha256 is None
        )
    else:
        matches = (
            command.decision_id == latest.decision_id
            and command.decision_content_sha256 == latest.decision_content_sha256
            and command.expected_condition_version == latest.version
            and command.previous_content_sha256 == latest.content_sha256
        )
    if not matches:
        raise ConditionError("candidate_condition_version_conflict", "核验版本或前驱摘要已变化")


def verification_availability(
    record: ConditionRecord,
    latest_version: int,
    decision_availability: DecisionAvailability,
    now: datetime,
    *,
    budget_current: bool,
) -> ConditionAvailability:
    """历史替代优先于期限；最新事实仍须原批准和原预算继续适用。"""

    if record.version < latest_version:
        return "superseded"
    if _utc(now) >= _utc(record.expires_at) or decision_availability == "expired":
        return "expired"
    if decision_availability != "current" or not budget_current:
        return "stale"
    return "current"


def expires_at(
    created_at: datetime,
    request_expires_at: datetime,
    items: tuple[ConditionInput, ...],
    preflight_expiries: tuple[datetime, ...],
) -> datetime:
    """核验期限不能越过原批准或任何本次引用来源的有效期。"""

    deadlines = [_utc(request_expires_at), _utc(created_at) + timedelta(seconds=300)]
    deadlines.extend(
        _utc(source.observed_at) + timedelta(seconds=300)
        for item in items
        for source in item.sources
        if isinstance(source, HumanObservation)
    )
    deadlines.extend(_utc(value) for value in preflight_expiries)
    return min(deadlines)
