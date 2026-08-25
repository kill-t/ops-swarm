"""审批请求创建、状态迁移和计划完整性的真实 PostgreSQL 测试。"""

import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.approval import (
    APPROVAL_REQUEST_TTL,
    ApprovalDecision,
    ApprovalPlanIntegrityViolation,
    ApprovalRequestAlreadyDecided,
    ApprovalRequestStatus,
)
from opsswarm.approval.persistence import (
    ApprovalDecisionOutcome,
    ApprovalRequestStore,
    StoredApprovalRequest,
)
from opsswarm.approval.persistence.tables import ApprovalRequestRow
from opsswarm.incidents import ActorType, IncidentActor, IncidentStatus
from opsswarm.incidents.errors import IncidentVersionConflict
from opsswarm.incidents.persistence import IncidentStore
from opsswarm.investigation.persistence import StartedInvestigation
from opsswarm.planning import ActionPlanStatus
from opsswarm.planning.persistence import ActionPlanStore, StoredActionPlan
from opsswarm.planning.persistence.tables import ActionPlanRow
from tests.integration.test_investigation_store import (
    BASE_TIME,
    make_outcome,
    start_investigation,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

HUMAN_ACTOR = IncidentActor(actor_type=ActorType.HUMAN, actor_id="engineer-001")


async def prepare_draft_plan(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    suffix: str,
) -> tuple[ActionPlanStore, StartedInvestigation, StoredActionPlan]:
    """完成一次固定调查并生成需要人工审批的高风险计划。"""

    investigation_store, started = await start_investigation(
        session_factory,
        suffix=suffix,
    )
    claimed = await investigation_store.claim_next(
        worker_id=f"approval-worker-{suffix}",
        occurred_at=BASE_TIME + timedelta(seconds=2),
        lease_duration=timedelta(seconds=30),
    )
    assert claimed is not None and claimed.work_item.lease_token is not None
    outcome = make_outcome()
    await investigation_store.complete(
        claimed.work_item.id,
        lease_token=claimed.work_item.lease_token,
        occurred_at=BASE_TIME + timedelta(seconds=3),
        result=outcome.result,
        evidence_items=outcome.evidence_items,
        specialist_reports=outcome.specialist_reports,
        hypothesis_challenge=outcome.hypothesis_challenge,
        metrics=outcome.metrics,
        model_name=outcome.model_name,
    )
    plan_store = ActionPlanStore(session_factory)
    creation = await plan_store.create_for_investigation(
        started.run.id,
        created_at=BASE_TIME + timedelta(seconds=4),
    )
    return plan_store, started, creation.plan


async def prepare_pending_request(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    suffix: str,
) -> tuple[
    ActionPlanStore,
    StartedInvestigation,
    StoredActionPlan,
    ApprovalRequestStore,
    StoredApprovalRequest,
]:
    plan_store, started, plan = await prepare_draft_plan(session_factory, suffix=suffix)
    approval_store = ApprovalRequestStore(session_factory)
    creation = await approval_store.create_for_action_plan(
        plan.id,
        expected_incident_version=2,
        occurred_at=BASE_TIME + timedelta(seconds=5),
        actor=HUMAN_ACTOR,
        reason="请求审批回滚计划",
    )
    return plan_store, started, plan, approval_store, creation.request


async def test_concurrent_submission_creates_one_request_and_one_state_transition(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    plan_store, started, plan = await prepare_draft_plan(
        database_session_factory,
        suffix="approval-concurrent",
    )
    approval_store = ApprovalRequestStore(database_session_factory)

    first, second = await asyncio.gather(
        *(
            approval_store.create_for_action_plan(
                plan.id,
                expected_incident_version=2,
                occurred_at=BASE_TIME + timedelta(seconds=5),
                actor=HUMAN_ACTOR,
                reason="请求审批回滚计划",
            )
            for _ in range(2)
        )
    )

    assert sorted((first.created, second.created)) == [False, True]
    assert first.request == second.request
    request = first.request
    assert request.status is ApprovalRequestStatus.PENDING
    assert request.action_plan_id == plan.id
    assert request.action_plan_version == 1
    assert len(request.plan_content_hash) == 64
    assert request.expires_at - request.created_at == APPROVAL_REQUEST_TTL
    assert await approval_store.get(request.id) == request

    current_plan = await plan_store.get_current(started.run.id)
    assert current_plan.status is ActionPlanStatus.AWAITING_APPROVAL
    incident_store = IncidentStore(database_session_factory)
    incident = await incident_store.get_incident(started.incident.incident.id)
    assert incident.incident.status is IncidentStatus.AWAITING_APPROVAL
    assert incident.incident.version == 3
    events = await incident_store.list_events(started.incident.incident.id)
    assert events[-1].event.to_status is IncidentStatus.AWAITING_APPROVAL
    assert events[-1].event.incident_version == 3

    async with database_session_factory() as session:
        count = await session.scalar(select(func.count()).select_from(ApprovalRequestRow))
    assert count == 1


async def test_stale_version_rolls_back_all_changes_and_exact_version_fk_is_enforced(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    plan_store, started, plan = await prepare_draft_plan(
        database_session_factory,
        suffix="approval-stale",
    )
    approval_store = ApprovalRequestStore(database_session_factory)

    with pytest.raises(IncidentVersionConflict):
        await approval_store.create_for_action_plan(
            plan.id,
            expected_incident_version=1,
            occurred_at=BASE_TIME + timedelta(seconds=5),
            actor=HUMAN_ACTOR,
            reason="使用过期版本请求审批",
        )

    current_plan = await plan_store.get_current(started.run.id)
    assert current_plan.status is ActionPlanStatus.DRAFT
    incident = await IncidentStore(database_session_factory).get_incident(
        started.incident.incident.id
    )
    assert incident.incident.status is IncidentStatus.INVESTIGATING
    assert incident.incident.version == 2
    async with database_session_factory() as session:
        count = await session.scalar(select(func.count()).select_from(ApprovalRequestRow))
    assert count == 0

    # 组合外键保证审批对象不是“某个计划”，而是计划的精确版本。
    async with database_session_factory() as session:
        session.add(
            ApprovalRequestRow(
                id=uuid4(),
                action_plan_id=plan.id,
                action_plan_version=2,
                plan_content_hash="0" * 64,
                status=ApprovalRequestStatus.PENDING.value,
                requested_by_type=ActorType.HUMAN.value,
                requested_by_id="engineer-001",
                request_reason="绑定不存在的计划版本",
                created_at=BASE_TIME + timedelta(seconds=5),
                expires_at=BASE_TIME + timedelta(seconds=5) + APPROVAL_REQUEST_TTL,
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_read_detects_plan_content_changed_after_submission(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _, _, plan = await prepare_draft_plan(
        database_session_factory,
        suffix="approval-integrity",
    )
    approval_store = ApprovalRequestStore(database_session_factory)
    creation = await approval_store.create_for_action_plan(
        plan.id,
        expected_incident_version=2,
        occurred_at=BASE_TIME + timedelta(seconds=5),
        actor=HUMAN_ACTOR,
        reason="请求审批回滚计划",
    )

    async with database_session_factory() as session, session.begin():
        await session.execute(
            update(ActionPlanRow)
            .where(ActionPlanRow.id == plan.id)
            .values(summary="未经审批的计划内容变更")
        )

    with pytest.raises(ApprovalPlanIntegrityViolation):
        await approval_store.get(creation.request.id)


async def test_approve_atomically_records_audit_and_is_idempotent(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    plan_store, started, _, approval_store, request = await prepare_pending_request(
        database_session_factory,
        suffix="decision-approve",
    )

    first = await approval_store.decide(
        request.id,
        decision=ApprovalDecision.APPROVE,
        expected_incident_version=3,
        occurred_at=BASE_TIME + timedelta(seconds=6),
        actor=HUMAN_ACTOR,
        reason="确认回滚版本和恢复判据",
    )
    duplicate = await approval_store.decide(
        request.id,
        decision=ApprovalDecision.APPROVE,
        expected_incident_version=3,
        occurred_at=BASE_TIME + timedelta(seconds=7),
        actor=HUMAN_ACTOR,
        reason="网络重试不应覆盖原决定",
    )

    assert first.decision_applied is True
    assert duplicate.decision_applied is False
    assert duplicate.request == first.request
    assert first.request.status is ApprovalRequestStatus.APPROVED
    assert first.request.decided_by_type is ActorType.HUMAN
    assert first.request.decided_by_id == "engineer-001"
    assert first.request.decision_reason == "确认回滚版本和恢复判据"
    assert first.request.decided_at == BASE_TIME + timedelta(seconds=6)
    assert (await plan_store.get_current(started.run.id)).status is ActionPlanStatus.APPROVED

    incident_store = IncidentStore(database_session_factory)
    incident = await incident_store.get_incident(started.incident.incident.id)
    assert incident.incident.status is IncidentStatus.MITIGATING
    assert incident.incident.version == 4
    events = await incident_store.list_events(started.incident.incident.id)
    assert events[-1].event.to_status is IncidentStatus.MITIGATING
    assert events[-1].event.actor == HUMAN_ACTOR


async def test_reject_returns_incident_to_investigation(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    plan_store, started, _, approval_store, request = await prepare_pending_request(
        database_session_factory,
        suffix="decision-reject",
    )
    reviewer = IncidentActor(actor_type=ActorType.HUMAN, actor_id="engineer-002")

    outcome = await approval_store.decide(
        request.id,
        decision=ApprovalDecision.REJECT,
        expected_incident_version=3,
        occurred_at=BASE_TIME + timedelta(seconds=6),
        actor=reviewer,
        reason="证据不足，需要继续调查",
    )

    assert outcome.decision_applied is True
    assert outcome.request.status is ApprovalRequestStatus.REJECTED
    assert outcome.request.decided_by_id == "engineer-002"
    assert (await plan_store.get_current(started.run.id)).status is ActionPlanStatus.REJECTED
    incident = await IncidentStore(database_session_factory).get_incident(
        started.incident.incident.id
    )
    assert incident.incident.status is IncidentStatus.INVESTIGATING
    assert incident.incident.version == 4


async def test_expired_request_is_system_terminal_and_cannot_be_approved(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    plan_store, started, _, approval_store, request = await prepare_pending_request(
        database_session_factory,
        suffix="decision-expired",
    )
    expired_at = request.expires_at + timedelta(seconds=1)

    outcome = await approval_store.decide(
        request.id,
        decision=ApprovalDecision.APPROVE,
        expected_incident_version=3,
        occurred_at=expired_at,
        actor=HUMAN_ACTOR,
        reason="到达时已经超过有效期",
    )

    assert outcome.decision_applied is False
    assert outcome.request.status is ApprovalRequestStatus.EXPIRED
    assert outcome.request.decided_by_type is ActorType.SYSTEM
    assert outcome.request.decided_by_id is None
    assert outcome.request.decided_at == expired_at
    assert (await plan_store.get_current(started.run.id)).status is ActionPlanStatus.EXPIRED
    incident_store = IncidentStore(database_session_factory)
    incident = await incident_store.get_incident(started.incident.incident.id)
    assert incident.incident.status is IncidentStatus.INVESTIGATING
    assert incident.incident.version == 4
    events = await incident_store.list_events(started.incident.incident.id)
    assert events[-1].event.actor.actor_type is ActorType.SYSTEM


async def test_opposite_concurrent_decisions_only_commit_one_terminal_state(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _, started, _, approval_store, request = await prepare_pending_request(
        database_session_factory,
        suffix="decision-race",
    )

    results = await asyncio.gather(
        *(
            approval_store.decide(
                request.id,
                decision=decision,
                expected_incident_version=3,
                occurred_at=BASE_TIME + timedelta(seconds=6),
                actor=HUMAN_ACTOR,
                reason=f"并发决定 {decision.value}",
            )
            for decision in (ApprovalDecision.APPROVE, ApprovalDecision.REJECT)
        ),
        return_exceptions=True,
    )

    outcomes = [result for result in results if isinstance(result, ApprovalDecisionOutcome)]
    conflicts = [result for result in results if isinstance(result, ApprovalRequestAlreadyDecided)]
    assert len(outcomes) == 1
    assert len(conflicts) == 1
    assert outcomes[0].decision_applied is True
    stored = await approval_store.get(request.id)
    assert stored.status in {ApprovalRequestStatus.APPROVED, ApprovalRequestStatus.REJECTED}
    incident_store = IncidentStore(database_session_factory)
    incident = await incident_store.get_incident(started.incident.incident.id)
    assert incident.incident.version == 4
    assert len(await incident_store.list_events(started.incident.incident.id)) == 4


async def test_database_rejects_terminal_status_without_decision_audit(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _, _, _, _, request = await prepare_pending_request(
        database_session_factory,
        suffix="decision-constraint",
    )

    async with database_session_factory() as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                update(ApprovalRequestRow)
                .where(ApprovalRequestRow.id == request.id)
                .values(status=ApprovalRequestStatus.APPROVED.value)
            )
