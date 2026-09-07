"""只从当前运行的业务事实加载上下文材料，不读取 checkpoint。"""

from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from opsswarm.incidents.persistence.tables import IncidentRow
from opsswarm.investigation.budget.contracts import CallOperation
from opsswarm.investigation.budget.policy import operation_role
from opsswarm.investigation.context.contracts import (
    CONTEXT_POLICY_VERSION,
    ContextSources,
    ContextTaskMaterial,
)
from opsswarm.investigation.models import InvestigationRequest
from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.investigation.tasks.artifact_tables import AgentTaskResultRow
from opsswarm.investigation.tasks.contracts import TaskAssignment
from opsswarm.investigation.tasks.errors import TaskFactConflict
from opsswarm.investigation.tasks.evidence import EvidencePolicyVersion
from opsswarm.investigation.tasks.evidence_store import validated_record
from opsswarm.investigation.tasks.evidence_tables import AgentTaskEvidenceRow
from opsswarm.investigation.tasks.results import TaskResultRecord
from opsswarm.investigation.tasks.tables import AgentTaskRow


async def load_sources(
    session: AsyncSession,
    run: InvestigationRunRow,
    operation: CallOperation,
    task_id: UUID | None,
) -> ContextSources:
    if run.context_policy_version != CONTEXT_POLICY_VERSION or run.evidence_policy_version not in (
        "task-evidence-v1",
        "task-evidence-live-v1",
    ):
        raise TaskFactConflict("context_policy_mismatch", "运行没有受支持的上下文策略")
    incident = await session.get(IncidentRow, run.incident_id)
    assert incident is not None
    role = operation_role(operation)
    if (role is None) != (task_id is None):
        raise TaskFactConflict("context_scope_mismatch", "角色与任务身份不匹配")
    query = select(AgentTaskRow).where(AgentTaskRow.investigation_run_id == run.id)
    if task_id is not None:
        query = query.where(AgentTaskRow.id == task_id)
    rows = list(await session.scalars(query.order_by(AgentTaskRow.task_key).limit(4)))
    if len(rows) != (1 if role else 3):
        raise TaskFactConflict("context_scope_mismatch", "上下文缺少完整的固定任务范围")
    tasks = []
    for row in rows:
        if row.status != ("running" if role else "succeeded"):
            raise TaskFactConflict("context_task_not_ready", "专业任务状态尚不能用于该模型调用")
        assignment = TaskAssignment.model_validate(row.assignment)
        evidence = await session.get(AgentTaskEvidenceRow, row.id)
        if evidence is None:
            raise TaskFactConflict("context_evidence_required", "模型调用前需要已提交取证材料")
        result = await session.get(AgentTaskResultRow, row.id) if role is None else None
        tasks.append(
            ContextTaskMaterial(
                task_id=row.id,
                assignment=assignment,
                evidence=validated_record(evidence, assignment, run),
                result=TaskResultRecord.model_validate(result, from_attributes=True)
                if result
                else None,
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
    )
