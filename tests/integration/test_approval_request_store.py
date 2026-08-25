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
    ApprovalPlanIntegrityViolation,
    ApprovalRequestStatus,
)
from opsswarm.approval.persistence import ApprovalRequestStore
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
