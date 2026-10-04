"""只从当前运行的业务事实加载上下文材料，不读取 checkpoint。"""

from typing import Literal, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from opsswarm.incidents.persistence.tables import IncidentRow
from opsswarm.investigation.budget.contracts import CallOperation
from opsswarm.investigation.budget.policy import operation_role
from opsswarm.investigation.context.contracts import (
    ContextPolicyVersion,
    ContextSources,
    ContextTaskMaterial,
)
from opsswarm.investigation.coordination.tables import InvestigationCoordinationPlanRow
from opsswarm.investigation.models import InvestigationRequest
from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.investigation.tasks.artifact_tables import AgentTaskResultRow
from opsswarm.investigation.tasks.contracts import TaskAssignment
from opsswarm.investigation.tasks.errors import TaskFactConflict
from opsswarm.investigation.tasks.evidence import EvidencePolicyVersion
from opsswarm.investigation.tasks.evidence_store import validated_record
from opsswarm.investigation.tasks.evidence_tables import AgentTaskEvidenceRow
from opsswarm.investigation.tasks.results import TaskResultRecord
from opsswarm.investigation.tasks.tables import AgentTaskRow, InvestigationRoundRow
from opsswarm.investigation.workflow_versions import (
    DYNAMIC_REVIEW_GRAPH_VERSIONS,
    DYNAMIC_SECOND_ROUND_REVIEW_GRAPH_VERSIONS,
    DYNAMIC_SECOND_ROUND_SYNTHESIS_GRAPH_VERSIONS,
    DYNAMIC_THIRD_ROUND_REVIEW_GRAPH_VERSIONS,
    DYNAMIC_THIRD_ROUND_SYNTHESIS_GRAPH_VERSIONS,
)
from opsswarm.investigation.working_summary.contracts import SUMMARY_VERSION
from opsswarm.investigation.working_summary.persistence import validated_summary
from opsswarm.investigation.working_summary.tables import AgentTaskWorkingSummaryRow


async def load_sources(
    session: AsyncSession,
    run: InvestigationRunRow,
    operation: CallOperation,
    task_id: UUID | None,
    *,
    invocation_index: int = 0,
) -> ContextSources:
    if operation == CallOperation.REVIEW and run.graph_version not in DYNAMIC_REVIEW_GRAPH_VERSIONS:
        raise TaskFactConflict(
            "hypothesis_review_context_not_supported",
            "只有当前动态审查图可以预留独立审查上下文",
        )
    if run.context_policy_version not in (
        "task-context-v1",
        "evidence-read-v1",
        "summary-context-v1",
    ) or run.evidence_policy_version not in (
        "task-evidence-v1",
        "task-evidence-live-v1",
    ):
        raise TaskFactConflict("context_policy_mismatch", "运行没有受支持的上下文策略")
    if (
        run.context_policy_version == "summary-context-v1"
        and run.working_summary_policy_version != SUMMARY_VERSION
    ):
        raise TaskFactConflict("context_policy_mismatch", "摘要上下文缺少固定的摘要策略")
    incident = await session.get(IncidentRow, run.incident_id)
    assert incident is not None
    role = operation_role(operation)
    if (role is None) != (task_id is None):
        raise TaskFactConflict("context_scope_mismatch", "角色与任务身份不匹配")
    query = select(AgentTaskRow).where(AgentTaskRow.investigation_run_id == run.id)
    if task_id is not None:
        query = query.where(AgentTaskRow.id == task_id)
    expected_count = 1 if role else 3
    open_hypothesis = None
    source_synthesis_manifest = None
    round_number = None
    if role is None and run.workflow_version == "v3":
        # 协调持久层也依赖上下文契约，延迟导入可避免模块初始化环。
        from opsswarm.investigation.coordination.persistence import validated_plan

        if operation == CallOperation.SYNTHESIZE:
            round_number = invocation_index + 1
            if (
                round_number not in (1, 2, 3)
                or (
                    round_number == 2
                    and run.graph_version not in DYNAMIC_SECOND_ROUND_SYNTHESIS_GRAPH_VERSIONS
                )
                or (
                    round_number == 3
                    and run.graph_version not in DYNAMIC_THIRD_ROUND_SYNTHESIS_GRAPH_VERSIONS
                )
            ):
                raise TaskFactConflict(
                    "context_scope_mismatch", "动态综合调用序号没有对应的受支持业务轮次"
                )
        elif operation == CallOperation.REVIEW:
            round_number = invocation_index + 1
            if (
                round_number not in (1, 2, 3)
                or (
                    round_number == 2
                    and run.graph_version not in DYNAMIC_SECOND_ROUND_REVIEW_GRAPH_VERSIONS
                )
                or (
                    round_number == 3
                    and run.graph_version not in DYNAMIC_THIRD_ROUND_REVIEW_GRAPH_VERSIONS
                )
            ):
                raise TaskFactConflict(
                    "context_scope_mismatch", "动态审查调用序号没有对应的受支持业务轮次"
                )
        else:
            raise TaskFactConflict("context_scope_mismatch", "动态根级调用缺少受支持的轮次范围")
        round_row = await session.scalar(
            select(InvestigationRoundRow).where(
                InvestigationRoundRow.investigation_run_id == run.id,
                InvestigationRoundRow.round_number == round_number,
            )
        )
        plan_row = (
            await session.get(InvestigationCoordinationPlanRow, round_row.id)
            if round_row is not None
            else None
        )
        if plan_row is None:
            raise TaskFactConflict("context_scope_mismatch", "动态根级调用缺少目标轮次协调计划")
        plan = (await validated_plan(session, plan_row)).plan
        expected_assignments = {item.task_key: item for item in plan.assignment.tasks}
        query = query.where(AgentTaskRow.round_id == plan_row.round_id)
        rows = list(await session.scalars(query.order_by(AgentTaskRow.task_key)))
        if (
            plan.assignment.round_number != round_number
            or {row.task_key for row in rows} != set(expected_assignments)
            or any(
                row.round_id != plan_row.round_id
                or TaskAssignment.model_validate(row.assignment)
                != expected_assignments[row.task_key]
                for row in rows
            )
        ):
            raise TaskFactConflict("context_scope_mismatch", "动态根级任务范围与目标计划不一致")
        expected_count = len(expected_assignments)
        if operation == CallOperation.REVIEW:
            # 审查持久层反向依赖上下文复核，延迟导入避免模块初始化环。
            from opsswarm.investigation.synthesis.persistence import (
                validated_open_hypothesis,
                validated_synthesis_call,
            )
            from opsswarm.investigation.synthesis.tables import InvestigationOpenHypothesisRow

            hypothesis_row = await session.scalar(
                select(InvestigationOpenHypothesisRow).where(
                    InvestigationOpenHypothesisRow.investigation_run_id == run.id,
                    InvestigationOpenHypothesisRow.round_id == plan_row.round_id,
                )
            )
            if hypothesis_row is None:
                raise TaskFactConflict("context_scope_mismatch", "独立审查缺少开放假设")
            open_hypothesis = await validated_open_hypothesis(session, hypothesis_row)
            _, _, source_synthesis_manifest = await validated_synthesis_call(
                session,
                run,
                open_hypothesis.source_call_id,
                expected_round_number=round_number,
            )
    else:
        rows = list(await session.scalars(query.order_by(AgentTaskRow.task_key)))
    if len(rows) != expected_count:
        raise TaskFactConflict("context_scope_mismatch", "上下文缺少完整的任务范围")
    tasks = []
    for row in rows:
        if row.status != ("running" if role else "succeeded"):
            raise TaskFactConflict("context_task_not_ready", "专业任务状态尚不能用于该模型调用")
        assignment = TaskAssignment.model_validate(row.assignment)
        evidence = await session.get(AgentTaskEvidenceRow, row.id)
        if evidence is None:
            raise TaskFactConflict("context_evidence_required", "模型调用前需要已提交取证材料")
        result = await session.get(AgentTaskResultRow, row.id) if role is None else None
        result_record = (
            TaskResultRecord.model_validate(result, from_attributes=True) if result else None
        )
        summary = None
        if run.context_policy_version == "summary-context-v1" and role is None:
            summary_row = await session.get(AgentTaskWorkingSummaryRow, row.id)
            if summary_row is None or result_record is None:
                raise TaskFactConflict("context_summary_required", "综合需要全部已选任务的工作摘要")
            summary = await validated_summary(session, row, result_record, summary_row)
        tasks.append(
            ContextTaskMaterial(
                task_id=row.id,
                assignment=assignment,
                evidence=validated_record(evidence, assignment, run),
                result=result_record,
                working_summary=summary,
            )
        )
    return ContextSources(
        run_id=run.id,
        task_id=task_id,
        request=InvestigationRequest(
            incident_id=incident.id,
            service_name=incident.service_name,
            title=incident.title,
            started_at=incident.started_at,
        ),
        evidence_policy_version=cast(EvidencePolicyVersion, run.evidence_policy_version),
        tasks=tuple(tasks),
        workflow_version=cast(Literal["v2", "v3"], run.workflow_version),
        context_policy_version=cast(ContextPolicyVersion, run.context_policy_version),
        round_number=round_number,
        open_hypothesis=open_hypothesis,
        source_synthesis_manifest=source_synthesis_manifest,
    )
