"""在父调查租约内固定协调策略，并原子保存计划与业务轮次。"""

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.incidents.persistence.tables import IncidentRow
from opsswarm.investigation.budget.model_contracts import ModelBudgetPolicy
from opsswarm.investigation.budget.tables import CallReservationRow
from opsswarm.investigation.context.contracts import ContextManifest
from opsswarm.investigation.coordination.context import validate_coordination_context
from opsswarm.investigation.coordination.contracts import (
    CoordinationModelInput,
    CoordinationModelResult,
    CoordinationPlanRecord,
    CoordinationPolicy,
    CoordinationPolicyRecord,
    CoordinationProposal,
)
from opsswarm.investigation.coordination.hashing import coordination_hash
from opsswarm.investigation.coordination.policy import compile_coordination_plan
from opsswarm.investigation.coordination.tables import (
    InvestigationCoordinationPlanRow,
    InvestigationCoordinationPolicyRow,
)
from opsswarm.investigation.models import InvestigationRequest
from opsswarm.investigation.persistence.lease import active_investigation_transaction
from opsswarm.investigation.persistence.tables import InvestigationAttemptRow, InvestigationRunRow
from opsswarm.investigation.tasks.contracts import TaskAssignment
from opsswarm.investigation.tasks.errors import TaskFactConflict
from opsswarm.investigation.tasks.identity import round_content_hash, round_specification
from opsswarm.investigation.tasks.store import create_round_in_transaction
from opsswarm.investigation.tasks.tables import AgentTaskRow, InvestigationRoundRow
from opsswarm.investigation.workflow_versions import DYNAMIC_INVESTIGATION_GRAPH_VERSIONS


def utc_now() -> datetime:
    return datetime.now(UTC)


def validated_policy(row: InvestigationCoordinationPolicyRow) -> CoordinationPolicyRecord:
    try:
        return CoordinationPolicyRecord.model_validate(row, from_attributes=True)
    except ValueError as error:
        raise TaskFactConflict(
            "coordination_policy_integrity_violation", "已保存协调策略无法通过完整性校验"
        ) from error


async def validated_plan(
    session: AsyncSession,
    row: InvestigationCoordinationPlanRow,
) -> CoordinationPlanRecord:
    """从业务来源重建计划，避免只相信同一行中的散列。"""

    try:
        record = CoordinationPlanRecord.model_validate(row, from_attributes=True)
        policy_row = await session.get(
            InvestigationCoordinationPolicyRow, record.investigation_run_id
        )
        round_row = await session.get(InvestigationRoundRow, record.round_id)
        if policy_row is None or round_row is None:
            raise ValueError("计划缺少策略或轮次")
        policy_record = validated_policy(policy_row)
        if (
            record.policy_sha256 != policy_record.content_sha256
            or record.plan.policy_version != policy_record.version
            or round_row.investigation_run_id != record.investigation_run_id
            or round_row.goal != record.plan.assignment.goal
            or round_row.created_at != record.created_at
            or round_row.specification != round_specification(record.plan.assignment)
            or round_row.content_hash != round_content_hash(record.plan.assignment)
        ):
            raise ValueError("计划与策略或轮次不一致")
        task_rows = list(
            await session.scalars(
                select(AgentTaskRow)
                .where(AgentTaskRow.round_id == record.round_id)
                .order_by(AgentTaskRow.task_key)
            )
        )
        expected_tasks = {task.task_key: task for task in record.plan.assignment.tasks}
        actual_tasks = {
            task.task_key: TaskAssignment.model_validate(task.assignment) for task in task_rows
        }
        if actual_tasks != expected_tasks:
            raise ValueError("计划与任务事实不一致")
        if any(
            task.max_attempts != TaskAssignment.model_validate(task.assignment).max_attempts
            for task in task_rows
        ):
            raise ValueError("任务执行上限与计划不一致")
        run = await session.get(InvestigationRunRow, record.investigation_run_id)
        incident = await session.get(IncidentRow, run.incident_id) if run is not None else None
        parent = await session.get(InvestigationAttemptRow, record.investigation_attempt_id)
        if (
            run is None
            or incident is None
            or parent is None
            or parent.investigation_run_id != record.investigation_run_id
            or not parent.started_at <= record.created_at <= parent.lease_expires_at
        ):
            raise ValueError("计划缺少事故或父尝试来源")
        prior_task_count = await _prior_task_count(
            session, record.investigation_run_id, record.plan.assignment.round_number
        )
        assignments = record.plan.assignment.tasks
        snapshot_id = assignments[0].evidence_snapshot_id
        cutoff = assignments[0].window_end
        if snapshot_id is None or any(
            task.evidence_snapshot_id != snapshot_id or task.window_end != cutoff
            for task in assignments
        ):
            raise ValueError("计划的系统注入范围不一致")
        rebuilt = compile_coordination_plan(
            record.plan.proposal,
            policy=policy_record.policy,
            request=_request(incident),
            expected_round_number=record.plan.assignment.round_number,
            existing_task_count=prior_task_count,
            observation_cutoff_at=cutoff,
            evidence_snapshot_id=snapshot_id,
        )
        if rebuilt != record.plan:
            raise ValueError("计划不能由持久来源重建")
        if record.source_call_id is not None:
            model_input, model_result, source = await validated_coordination_call(
                session, run, record.source_call_id
            )
            if (
                model_result.proposal != record.plan.proposal
                or model_input.round_number != record.plan.assignment.round_number
                or model_input.observation_cutoff_at != cutoff
                or model_input.evidence_snapshot_id != snapshot_id
                or source.finished_at is None
                or source.finished_at > record.created_at
            ):
                raise ValueError("计划与协调模型调用来源不一致")
        return record
    except (TaskFactConflict, ValueError) as error:
        if isinstance(error, TaskFactConflict) and error.code.startswith("coordination_policy_"):
            raise
        raise TaskFactConflict(
            "coordination_plan_integrity_violation", "已保存协调计划无法通过完整性校验"
        ) from error


def _request(incident: IncidentRow) -> InvestigationRequest:
    return InvestigationRequest(
        incident_id=incident.id,
        service_name=incident.service_name,
        title=incident.title,
        started_at=incident.started_at,
    )


async def _prior_task_count(session: AsyncSession, run_id: UUID, round_number: int) -> int:
    return int(
        await session.scalar(
            select(func.count())
            .select_from(AgentTaskRow)
            .join(InvestigationRoundRow, InvestigationRoundRow.id == AgentTaskRow.round_id)
            .where(
                InvestigationRoundRow.investigation_run_id == run_id,
                InvestigationRoundRow.round_number < round_number,
            )
        )
        or 0
    )


async def validated_coordination_call(
    session: AsyncSession,
    run: InvestigationRunRow,
    source_call_id: UUID,
) -> tuple[CoordinationModelInput, CoordinationModelResult, CallReservationRow]:
    """复核成功协调调用的范围、上下文、分配和结构化结果。"""

    try:
        row = await session.get(CallReservationRow, source_call_id)
        parent = (
            await session.get(InvestigationAttemptRow, row.investigation_attempt_id)
            if row is not None
            else None
        )
        if (
            row is None
            or parent is None
            or row.investigation_run_id != run.id
            or parent.investigation_run_id != run.id
            or row.operation != "coordinate"
            or row.kind != "model"
            or row.status != "succeeded"
            or row.task_id is not None
            or row.task_attempt_id is not None
            or row.model_input is None
            or row.model_allocation is None
            or row.context_manifest is None
            or row.call_result is None
            or row.finished_at is None
            or not parent.started_at
            <= row.reserved_at
            <= row.finished_at
            <= parent.lease_expires_at
        ):
            raise ValueError("协调调用范围或状态不合法")
        model_input = CoordinationModelInput.model_validate(row.model_input)
        result = CoordinationModelResult.model_validate(row.call_result)
        manifest = ContextManifest.model_validate(row.context_manifest)
        if (
            row.model_input != model_input.model_dump(mode="json")
            or row.call_result != result.model_dump(mode="json")
            or row.context_manifest != manifest.model_dump(mode="json")
            or row.model_allocation != manifest.allocation.model_dump(mode="json")
        ):
            raise ValueError("协调调用内容未经规范化保存或与分配不一致")
        await validate_coordination_context(session, run, model_input, manifest)
        return model_input, result, row
    except (TaskFactConflict, ValueError) as error:
        raise TaskFactConflict(
            "coordination_call_integrity_violation", "协调模型调用来源无法通过完整性校验"
        ) from error


async def _fix_policy(
    session: AsyncSession,
    run_id: UUID,
    policy: CoordinationPolicy,
    now: datetime,
    rounds: list[InvestigationRoundRow],
) -> tuple[CoordinationPolicyRecord, bool]:
    policy_sha256 = coordination_hash(policy)
    policy_row = await session.get(InvestigationCoordinationPolicyRow, run_id)
    if policy_row is None:
        if rounds:
            raise TaskFactConflict(
                "coordination_history_conflict", "已有固定轮次的运行不能改为动态协调历史"
            )
        policy_row = InvestigationCoordinationPolicyRow(
            investigation_run_id=run_id,
            version=policy.version,
            policy=policy.model_dump(mode="json"),
            content_sha256=policy_sha256,
            created_at=now,
        )
        session.add(policy_row)
        await session.flush()
        return validated_policy(policy_row), True
    saved_policy = validated_policy(policy_row)
    if saved_policy.policy != policy or saved_policy.content_sha256 != policy_sha256:
        raise TaskFactConflict("coordination_policy_conflict", "该运行已固定另一份协调策略")
    planned_round_ids = set(
        await session.scalars(
            select(InvestigationCoordinationPlanRow.round_id).where(
                InvestigationCoordinationPlanRow.investigation_run_id == run_id
            )
        )
    )
    if planned_round_ids != {item.id for item in rounds}:
        raise TaskFactConflict("coordination_history_conflict", "动态协调历史缺少轮次计划来源")
    return saved_policy, False


class CoordinationPlanStore:
    """模型和网络在此边界外运行；本服务只提交已校验业务事实。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock

    async def fix_policy(
        self,
        run_id: UUID,
        *,
        lease_token: UUID,
        policy: CoordinationPolicy,
    ) -> tuple[CoordinationPolicyRecord, bool]:
        """在任何外部协调调用前固定运行策略。"""

        async with active_investigation_transaction(
            self._session_factory, run_id, lease_token, self._clock
        ) as (session, _, _, _, now):
            rounds = list(
                await session.scalars(
                    select(InvestigationRoundRow)
                    .where(InvestigationRoundRow.investigation_run_id == run_id)
                    .order_by(InvestigationRoundRow.round_number)
                )
            )
            return await _fix_policy(session, run_id, policy, now, rounds)

    async def fix_model_policy(
        self,
        run_id: UUID,
        *,
        lease_token: UUID,
        policy: CoordinationPolicy,
    ) -> tuple[CoordinationPolicyRecord, bool]:
        """验证协调模型前置条件，并在同一租约事务内固定策略。"""

        async with active_investigation_transaction(
            self._session_factory, run_id, lease_token, self._clock
        ) as (session, _, _, run, now):
            try:
                model_policy = (
                    ModelBudgetPolicy.model_validate(run.model_budget)
                    if run.model_budget is not None
                    else None
                )
            except ValueError as error:
                raise TaskFactConflict(
                    "coordination_context_integrity_violation", "协调模型预算无法通过校验"
                ) from error
            if (
                run.workflow_version not in ("v2", "v3")
                or run.data_source_mode != "frozen"
                or run.evidence_policy_version != "task-evidence-v1"
                or run.call_budget is None
                or model_policy is None
                or model_policy.profile.structured_output_method != "function_calling"
                or (
                    run.workflow_version == "v3"
                    and (
                        run.graph_version not in DYNAMIC_INVESTIGATION_GRAPH_VERSIONS
                        or run.coordination_policy_version != "dynamic-coordination-v1"
                        or run.context_policy_version != "summary-context-v1"
                    )
                )
            ):
                raise TaskFactConflict(
                    "coordination_context_not_supported",
                    "协调模型只支持契约完整且已配置共享预算的 v2 或 v3 冻结运行",
                )
            rounds = list(
                await session.scalars(
                    select(InvestigationRoundRow)
                    .where(InvestigationRoundRow.investigation_run_id == run_id)
                    .order_by(InvestigationRoundRow.round_number)
                )
            )
            return await _fix_policy(session, run_id, policy, now, rounds)

    async def create_plan(
        self,
        run_id: UUID,
        *,
        lease_token: UUID,
        policy: CoordinationPolicy,
        proposal: CoordinationProposal,
        observation_cutoff_at: datetime,
        evidence_snapshot_id: str,
        source_call_id: UUID | None = None,
    ) -> tuple[CoordinationPlanRecord, bool]:
        """首次保存固定运行策略；同轮相同内容重放返回原计划。"""

        async with active_investigation_transaction(
            self._session_factory, run_id, lease_token, self._clock
        ) as (session, parent, incident, run, now):
            rounds = list(
                await session.scalars(
                    select(InvestigationRoundRow)
                    .where(InvestigationRoundRow.investigation_run_id == run_id)
                    .order_by(InvestigationRoundRow.round_number)
                )
            )
            existing_round = next(
                (item for item in rounds if item.round_number == proposal.round_number), None
            )
            expected_round_number = (
                proposal.round_number if existing_round is not None else len(rounds) + 1
            )
            plan = compile_coordination_plan(
                proposal,
                policy=policy,
                request=_request(incident),
                expected_round_number=expected_round_number,
                existing_task_count=await _prior_task_count(session, run_id, proposal.round_number),
                observation_cutoff_at=observation_cutoff_at,
                evidence_snapshot_id=evidence_snapshot_id,
            )
            policy_record, _ = await _fix_policy(session, run_id, policy, now, rounds)
            policy_sha256 = policy_record.content_sha256
            if source_call_id is not None:
                model_input, model_result, _ = await validated_coordination_call(
                    session, run, source_call_id
                )
                if (
                    model_result.proposal != proposal
                    or model_input.round_number != proposal.round_number
                    or model_input.observation_cutoff_at != observation_cutoff_at
                    or model_input.evidence_snapshot_id != evidence_snapshot_id
                ):
                    raise TaskFactConflict(
                        "coordination_call_mismatch", "协调调用与待提交计划参数不一致"
                    )
            if existing_round is not None:
                existing_plan = await session.get(
                    InvestigationCoordinationPlanRow, existing_round.id
                )
                if existing_plan is None:
                    raise TaskFactConflict(
                        "coordination_plan_missing", "该轮次不是由动态协调计划创建"
                    )
                record = await validated_plan(session, existing_plan)
                if (
                    record.plan != plan
                    or record.policy_sha256 != policy_sha256
                    or record.source_call_id != source_call_id
                ):
                    raise TaskFactConflict(
                        "coordination_plan_conflict", "该轮次已绑定另一份协调计划"
                    )
                return record, False
            round_id, created = await create_round_in_transaction(
                session,
                run_id=run_id,
                incident=incident,
                assignment=plan.assignment,
                now=now,
            )
            if not created:
                raise TaskFactConflict(
                    "coordination_plan_conflict", "轮次竞争未返回已持久化协调计划"
                )
            row = InvestigationCoordinationPlanRow(
                round_id=round_id,
                investigation_run_id=run_id,
                investigation_attempt_id=parent.id,
                source_call_id=source_call_id,
                version=plan.version,
                policy_sha256=policy_sha256,
                plan=plan.model_dump(mode="json"),
                content_sha256=coordination_hash(plan),
                created_at=now,
            )
            session.add(row)
            await session.flush()
            return await validated_plan(session, row), True
