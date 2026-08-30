"""处置计划的 PostgreSQL 幂等生成与读取。"""

from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.incidents.persistence.tables import IncidentRow
from opsswarm.investigation.errors import InvestigationRunNotFound
from opsswarm.investigation.models import (
    EvidenceKind,
    EvidenceObservation,
    HypothesisStatus,
    InvestigationRunResult,
    InvestigationRunStatus,
    RootCause,
)
from opsswarm.investigation.persistence.tables import (
    EvidenceItemRow,
    HypothesisRow,
    InvestigationRunRow,
)
from opsswarm.planning.capabilities import assess_action_plan_capability
from opsswarm.planning.errors import ActionPlanNotFound, ActionPlanNotReady
from opsswarm.planning.models import (
    ActionParameters,
    ActionPlan,
    ActionPlanCapability,
    ActionPlanStatus,
    ActionStep,
    ActionType,
    RestoreDependencyParameters,
    RiskLevel,
    RollbackConfigurationParameters,
    RollbackReleaseParameters,
    TargetType,
    VerificationRule,
)
from opsswarm.planning.persistence.records import (
    ActionPlanCreation,
    StoredActionPlan,
    StoredActionStep,
)
from opsswarm.planning.persistence.tables import ActionPlanRow, ActionStepRow
from opsswarm.planning.planner import propose_action_plan
from opsswarm.planning.policy import validate_action_plan_policy


class ActionPlanStore:
    """只生成和读取计划草案，不创建审批或执行记录。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create_for_investigation(
        self,
        run_id: UUID,
        *,
        created_at: datetime,
    ) -> ActionPlanCreation:
        """从已提交事实生成版本 1；并发重复请求返回同一记录。"""

        created_at = _normalize_time(created_at)
        async with self._session_factory() as session, session.begin():
            run_row, hypothesis_row = await _load_planning_source(session, run_id)

            # 注入时钟可能短暂回拨，计划时间不能早于其来源假设。
            created_at = max(created_at, hypothesis_row.created_at)

            existing = await _current_plan_row(session, run_id)
            if existing is not None:
                return ActionPlanCreation(
                    plan=await load_action_plan_in_session(session, existing),
                    created=False,
                )

            capability = _capability_from_rows(run_row, hypothesis_row)

            incident_row = await session.get(IncidentRow, run_row.incident_id)
            if incident_row is None:
                raise RuntimeError("调查关联的 Incident 不存在")
            evidence_rows = list(
                await session.scalars(
                    select(EvidenceItemRow)
                    .where(EvidenceItemRow.investigation_run_id == run_id)
                    .order_by(EvidenceItemRow.evidence_key)
                )
            )
            plan = propose_action_plan(
                service_name=incident_row.service_name,
                root_cause=RootCause(hypothesis_row.root_cause),
                recommendation=capability.recommendation,
                hypothesis_status=HypothesisStatus(hypothesis_row.status),
                evidence_items=tuple(_evidence_observation(row) for row in evidence_rows),
                version=1,
            )
            validate_action_plan_policy(plan)

            plan_id = uuid5(hypothesis_row.id, f"action-plan:{plan.version}")
            inserted_id = await session.scalar(
                postgresql_insert(ActionPlanRow)
                .values(
                    id=plan_id,
                    investigation_run_id=run_id,
                    hypothesis_id=hypothesis_row.id,
                    version=plan.version,
                    status=plan.status.value,
                    summary=plan.summary,
                    risk_level=plan.risk_level.value,
                    requires_human_approval=plan.requires_human_approval,
                    risk_policy_version=plan.risk_policy_version,
                    created_at=created_at,
                )
                .on_conflict_do_nothing()
                .returning(ActionPlanRow.id)
            )
            if inserted_id is None:
                concurrent = await _current_plan_row(session, run_id)
                if concurrent is None:
                    raise RuntimeError("并发计划已生成但无法读取")
                return ActionPlanCreation(
                    plan=await load_action_plan_in_session(session, concurrent),
                    created=False,
                )

            session.add_all(
                [
                    ActionStepRow(
                        id=uuid5(plan_id, f"step:{step.step_order}"),
                        action_plan_id=plan_id,
                        step_order=step.step_order,
                        action_type=step.action_type.value,
                        target_type=step.target_type.value,
                        target_name=step.target_name,
                        parameters=step.parameters.model_dump(mode="json"),
                        risk_level=step.risk_level.value,
                        requires_human_approval=step.requires_human_approval,
                        risk_rule_code=step.risk_rule_code,
                        risk_rationale=step.risk_rationale,
                        verification=step.verification.model_dump(mode="json"),
                        created_at=created_at,
                    )
                    for step in plan.steps
                ]
            )
            await session.flush()
            created_row = await session.get(ActionPlanRow, plan_id)
            if created_row is None:
                raise RuntimeError("处置计划写入后无法读取")
            return ActionPlanCreation(
                plan=await load_action_plan_in_session(session, created_row),
                created=True,
            )

    async def get_capability(self, run_id: UUID) -> ActionPlanCapability:
        """读取调查事实并返回可重算的计划模板能力，不创建任何记录。"""

        async with self._session_factory() as session:
            run_row, hypothesis_row = await _load_planning_source(session, run_id)
            return _capability_from_rows(run_row, hypothesis_row)

    async def get_current(self, run_id: UUID) -> StoredActionPlan:
        """读取调查当前最高版本计划。"""

        async with self._session_factory() as session:
            run_row = await session.get(InvestigationRunRow, run_id)
            if run_row is None:
                raise InvestigationRunNotFound(run_id)
            plan_row = await _current_plan_row(session, run_id)
            if plan_row is None:
                raise ActionPlanNotFound(run_id)
            return await load_action_plan_in_session(session, plan_row)


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


async def _load_planning_source(
    session: AsyncSession,
    run_id: UUID,
) -> tuple[InvestigationRunRow, HypothesisRow]:
    run_row = await session.get(InvestigationRunRow, run_id)
    if run_row is None:
        raise InvestigationRunNotFound(run_id)
    if run_row.status != InvestigationRunStatus.SUCCEEDED.value:
        raise ActionPlanNotReady(
            run_id,
            reason_code="investigation_not_succeeded",
            message="调查尚未成功完成，不能判断处置计划能力",
        )
    hypothesis_row = await session.scalar(
        select(HypothesisRow).where(HypothesisRow.investigation_run_id == run_id)
    )
    if hypothesis_row is None:
        raise ActionPlanNotReady(
            run_id,
            reason_code="hypothesis_missing",
            message="调查没有已提交的主根因假设",
        )
    return run_row, hypothesis_row


def _capability_from_rows(
    run_row: InvestigationRunRow,
    hypothesis_row: HypothesisRow,
) -> ActionPlanCapability:
    if run_row.result is None:
        raise ActionPlanNotReady(
            run_row.id,
            reason_code="investigation_result_missing",
            message="成功调查缺少结构化结果，不能判断处置计划能力",
        )
    result = InvestigationRunResult.model_validate(run_row.result)
    root_cause = RootCause(hypothesis_row.root_cause)
    if result.conclusion.root_cause is not root_cause:
        raise ActionPlanNotReady(
            run_row.id,
            reason_code="conclusion_hypothesis_mismatch",
            message="调查结论与主根因假设不一致，不能判断处置计划能力",
        )
    return assess_action_plan_capability(
        root_cause=root_cause,
        recommendation=result.conclusion.recommendation,
        hypothesis_status=HypothesisStatus(hypothesis_row.status),
    )


async def load_action_plan_in_session(
    session: AsyncSession,
    row: ActionPlanRow,
) -> StoredActionPlan:
    """在调用方事务中加载并重验一个完整计划版本。"""
    step_rows = list(
        await session.scalars(
            select(ActionStepRow)
            .where(ActionStepRow.action_plan_id == row.id)
            .order_by(ActionStepRow.step_order)
        )
    )
    plan = ActionPlan(
        version=row.version,
        status=ActionPlanStatus(row.status),
        summary=row.summary,
        risk_level=RiskLevel(row.risk_level),
        requires_human_approval=row.requires_human_approval,
        risk_policy_version=row.risk_policy_version,
        steps=tuple(_action_step(row) for row in step_rows),
    )
    validate_action_plan_policy(plan)
    return StoredActionPlan(
        id=row.id,
        investigation_run_id=row.investigation_run_id,
        hypothesis_id=row.hypothesis_id,
        version=plan.version,
        status=plan.status,
        summary=plan.summary,
        risk_level=plan.risk_level,
        requires_human_approval=plan.requires_human_approval,
        risk_policy_version=plan.risk_policy_version,
        steps=tuple(
            StoredActionStep(
                id=step_row.id,
                **step.model_dump(),
                created_at=step_row.created_at,
            )
            for step_row, step in zip(step_rows, plan.steps, strict=True)
        ),
        created_at=row.created_at,
    )


def _action_step(row: ActionStepRow) -> ActionStep:
    action_type = ActionType(row.action_type)
    return ActionStep(
        step_order=row.step_order,
        action_type=action_type,
        target_type=TargetType(row.target_type),
        target_name=row.target_name,
        parameters=_action_parameters(action_type, row.parameters),
        risk_level=RiskLevel(row.risk_level),
        requires_human_approval=row.requires_human_approval,
        risk_rule_code=row.risk_rule_code,
        risk_rationale=row.risk_rationale,
        verification=VerificationRule.model_validate(row.verification),
    )


def _action_parameters(
    action_type: ActionType,
    parameters: dict[str, object],
) -> ActionParameters:
    if action_type is ActionType.ROLLBACK_RELEASE:
        return RollbackReleaseParameters.model_validate(parameters)
    if action_type is ActionType.ROLLBACK_CONFIGURATION:
        return RollbackConfigurationParameters.model_validate(parameters)
    if action_type is ActionType.RESTORE_DEPENDENCY:
        return RestoreDependencyParameters.model_validate(parameters)
    raise ValueError(f"未知处置动作参数：{action_type.value}")


def _evidence_observation(row: EvidenceItemRow) -> EvidenceObservation:
    return EvidenceObservation(
        evidence_id=row.evidence_key,
        kind=EvidenceKind(row.kind),
        source=row.source,
        summary=row.summary,
        observed_at=row.observed_at,
        attributes=row.attributes,
    )


def _normalize_time(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("created_at 必须包含时区")
    return value.astimezone(UTC)
