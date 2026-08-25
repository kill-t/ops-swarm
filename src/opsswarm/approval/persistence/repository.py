"""审批请求、计划状态和 Incident 状态的 PostgreSQL 原子事务。"""

from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.approval.errors import (
    ActionPlanDoesNotRequireApproval,
    ActionPlanNotCurrent,
    ActionPlanNotSubmittable,
    ApprovalActionPlanNotFound,
    ApprovalPlanIntegrityViolation,
    ApprovalRequesterMustBeHuman,
    ApprovalRequestNotFound,
)
from opsswarm.approval.models import APPROVAL_REQUEST_TTL, ApprovalRequestStatus
from opsswarm.approval.persistence.records import (
    ApprovalRequestCreation,
    StoredApprovalRequest,
)
from opsswarm.approval.persistence.tables import ApprovalRequestRow
from opsswarm.incidents import ActorType, IncidentAction, IncidentActor
from opsswarm.incidents.errors import IncidentNotFound
from opsswarm.incidents.persistence.repository import transition_incident_in_session
from opsswarm.incidents.persistence.tables import IncidentRow
from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.planning import action_plan_content_hash
from opsswarm.planning.models import ActionPlanStatus
from opsswarm.planning.persistence import load_action_plan_in_session
from opsswarm.planning.persistence.records import StoredActionPlan
from opsswarm.planning.persistence.tables import ActionPlanRow


class ApprovalRequestStore:
    """创建和读取审批请求，不接受批准或拒绝决定。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create_for_action_plan(
        self,
        plan_id: UUID,
        *,
        expected_incident_version: int,
        occurred_at: datetime,
        actor: IncidentActor,
        reason: str,
    ) -> ApprovalRequestCreation:
        """原子创建 pending 请求并迁移计划与 Incident；重复请求幂等返回。"""

        occurred_at = _normalize_time(occurred_at)
        reason = _normalize_reason(reason)
        if actor.actor_type is not ActorType.HUMAN:
            raise ApprovalRequesterMustBeHuman("审批请求必须由人工操作者发起")
        if actor.actor_id is None:
            raise ApprovalRequesterMustBeHuman("审批请求必须提供人工操作者标识")

        async with self._session_factory() as session, session.begin():
            plan_row = await session.get(ActionPlanRow, plan_id)
            if plan_row is None:
                raise ApprovalActionPlanNotFound(plan_id)
            run_row = await session.get(InvestigationRunRow, plan_row.investigation_run_id)
            if run_row is None:
                raise RuntimeError("处置计划关联的调查运行不存在")
            incident_row = cast(
                IncidentRow | None,
                await session.scalar(
                    select(IncidentRow)
                    .where(IncidentRow.id == run_row.incident_id)
                    .with_for_update()
                ),
            )
            if incident_row is None:
                raise IncidentNotFound(str(run_row.incident_id))

            existing = await session.scalar(
                select(ApprovalRequestRow).where(ApprovalRequestRow.action_plan_id == plan_id)
            )
            if existing is not None:
                return ApprovalRequestCreation(
                    request=await _stored_request(session, existing),
                    created=False,
                )

            current_plan = await _current_plan_row(session, plan_row.investigation_run_id)
            if current_plan is None:
                raise ApprovalActionPlanNotFound(plan_id)
            if current_plan.id != plan_id:
                raise ActionPlanNotCurrent(plan_id, current_plan_id=current_plan.id)
            if plan_row.status != ActionPlanStatus.DRAFT.value:
                raise ActionPlanNotSubmittable(plan_id, status=plan_row.status)

            stored_plan = await load_action_plan_in_session(session, plan_row)
            if not stored_plan.requires_human_approval:
                raise ActionPlanDoesNotRequireApproval("处置计划不需要人工审批")
            occurred_at = max(occurred_at, stored_plan.created_at, incident_row.updated_at)
            content_hash = _content_hash(stored_plan)

            await transition_incident_in_session(
                session,
                run_row.incident_id,
                action=IncidentAction.REQUEST_APPROVAL,
                expected_version=expected_incident_version,
                occurred_at=occurred_at,
                actor=actor,
                reason=reason,
            )
            plan_row.status = ActionPlanStatus.AWAITING_APPROVAL.value
            request_row = ApprovalRequestRow(
                id=uuid5(plan_id, "approval-request:1"),
                action_plan_id=plan_id,
                action_plan_version=plan_row.version,
                plan_content_hash=content_hash,
                status=ApprovalRequestStatus.PENDING.value,
                requested_by_type=actor.actor_type.value,
                requested_by_id=actor.actor_id,
                request_reason=reason,
                expires_at=occurred_at + APPROVAL_REQUEST_TTL,
                created_at=occurred_at,
            )
            session.add(request_row)
            await session.flush()
            return ApprovalRequestCreation(
                request=await _stored_request(session, request_row),
                created=True,
            )

    async def get(self, request_id: UUID) -> StoredApprovalRequest:
        """读取请求并重算计划摘要，阻止审批对象静默变化。"""

        async with self._session_factory() as session:
            row = await session.get(ApprovalRequestRow, request_id)
            if row is None:
                raise ApprovalRequestNotFound(request_id)
            return await _stored_request(session, row)


async def _current_plan_row(session: AsyncSession, run_id: UUID) -> ActionPlanRow | None:
    return cast(
        ActionPlanRow | None,
        await session.scalar(
            select(ActionPlanRow)
            .where(ActionPlanRow.investigation_run_id == run_id)
            .order_by(ActionPlanRow.version.desc())
            .limit(1)
        ),
    )


async def _stored_request(
    session: AsyncSession,
    row: ApprovalRequestRow,
) -> StoredApprovalRequest:
    plan_row = await session.get(ActionPlanRow, row.action_plan_id)
    if plan_row is None:
        raise RuntimeError("审批请求关联的处置计划不存在")
    run_row = await session.get(InvestigationRunRow, plan_row.investigation_run_id)
    if run_row is None:
        raise RuntimeError("审批请求关联的调查运行不存在")
    stored_plan = await load_action_plan_in_session(session, plan_row)
    if _content_hash(stored_plan) != row.plan_content_hash:
        raise ApprovalPlanIntegrityViolation(row.id)
    return StoredApprovalRequest(
        id=row.id,
        incident_id=run_row.incident_id,
        action_plan_id=row.action_plan_id,
        action_plan_version=row.action_plan_version,
        plan_content_hash=row.plan_content_hash,
        status=ApprovalRequestStatus(row.status),
        requested_by_type=ActorType(row.requested_by_type),
        requested_by_id=row.requested_by_id,
        request_reason=row.request_reason,
        expires_at=row.expires_at,
        created_at=row.created_at,
    )


def _content_hash(plan: StoredActionPlan) -> str:
    return action_plan_content_hash(
        plan.to_domain(),
        plan_id=plan.id,
        investigation_run_id=plan.investigation_run_id,
        hypothesis_id=plan.hypothesis_id,
    )


def _normalize_time(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("occurred_at 必须包含时区")
    return value.astimezone(UTC)


def _normalize_reason(value: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > 500:
        raise ValueError("reason 长度必须在 1 到 500 之间")
    return normalized
