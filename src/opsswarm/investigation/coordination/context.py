"""为首轮协调调用组装并复核可追溯的最小上下文。"""

import json
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from langchain_core.utils.function_calling import convert_to_openai_tool
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.incidents.persistence.tables import IncidentRow
from opsswarm.investigation.budget.model_contracts import (
    ModelBudgetPolicy,
    TextModelMessage,
)
from opsswarm.investigation.budget.model_policy import allocate_input
from opsswarm.investigation.context.contracts import (
    ContextManifest,
    ContextMaterial,
    ContextPacket,
)
from opsswarm.investigation.coordination.contracts import (
    CoordinationModelInput,
    CoordinationPolicyRecord,
    CoordinationProposal,
)
from opsswarm.investigation.coordination.hashing import coordination_hash
from opsswarm.investigation.coordination.tables import InvestigationCoordinationPolicyRow
from opsswarm.investigation.errors import InvestigationRunNotFound
from opsswarm.investigation.models import InvestigationRequest
from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.investigation.tasks.errors import TaskFactConflict
from opsswarm.investigation.tasks.tables import AgentTaskRow, InvestigationRoundRow

COORDINATION_PROMPT_VERSION: Literal["coordination-plan-v1"] = "coordination-plan-v1"
_SYSTEM_PROMPT = (
    "你是事故调查协调 Agent。仅返回结构化调查提案；只能选择给定角色、目标和回看秒数。"
    "服务、时间边界、快照、重试、预算和工具权限由系统决定。"
)
_ROLE_WINDOWS = {"log": 900, "metric": 900, "change": 21600}


def _request(incident: IncidentRow) -> InvestigationRequest:
    return InvestigationRequest(
        incident_id=incident.id,
        service_name=incident.service_name,
        title=incident.title,
        started_at=incident.started_at,
    )


def build_coordination_context(
    request: InvestigationRequest,
    policy_record: CoordinationPolicyRecord,
    *,
    round_number: int,
    existing_task_count: int,
    observation_cutoff_at: datetime,
    evidence_snapshot_id: str,
    model_policy: ModelBudgetPolicy,
) -> ContextPacket:
    if observation_cutoff_at.tzinfo is None or observation_cutoff_at.utcoffset() is None:
        raise TaskFactConflict("coordination_window_invalid", "协调观察截止时间必须带时区")
    observation_cutoff_at = observation_cutoff_at.astimezone(UTC)
    if observation_cutoff_at < request.started_at:
        raise TaskFactConflict("coordination_window_invalid", "协调观察截止时间早于事故开始时间")
    if (
        evidence_snapshot_id != evidence_snapshot_id.strip()
        or not 1 <= len(evidence_snapshot_id) <= 100
    ):
        raise TaskFactConflict("coordination_snapshot_invalid", "协调证据快照标识不合法")
    user_payload = {
        "incident": request.model_dump(mode="json"),
        "round_number": round_number,
        "existing_task_count": existing_task_count,
        "policy": policy_record.policy.model_dump(mode="json"),
        "role_max_lookback_seconds": _ROLE_WINDOWS,
    }
    model_input = CoordinationModelInput(
        round_number=round_number,
        existing_task_count=existing_task_count,
        observation_cutoff_at=observation_cutoff_at,
        evidence_snapshot_id=evidence_snapshot_id,
        messages=(
            TextModelMessage(role="system", content=_SYSTEM_PROMPT),
            TextModelMessage(
                role="user",
                content=json.dumps(
                    user_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            ),
        ),
        output_schema=CoordinationProposal.model_json_schema(),
        tool_schema=convert_to_openai_tool(CoordinationProposal),
        structured_output_method="function_calling",
    )
    allocation = allocate_input(
        model_policy, model_input, prompt_version=COORDINATION_PROMPT_VERSION
    )
    return ContextPacket(
        model_input=model_input,
        manifest=ContextManifest(
            version="coordination-context-v1",
            investigation_run_id=policy_record.investigation_run_id,
            task_id=None,
            role="coordinator",
            role_version="dynamic-coordinator-v1",
            workflow_version="v2",
            prompt_version=COORDINATION_PROMPT_VERSION,
            evidence_policy_version="task-evidence-v1",
            materials=(
                ContextMaterial(
                    kind="incident_input",
                    material_id=request.incident_id,
                    version="investigation-request-v1",
                    content_sha256=coordination_hash(request),
                ),
                ContextMaterial(
                    kind="coordination_policy",
                    material_id=policy_record.investigation_run_id,
                    version=policy_record.version,
                    content_sha256=policy_record.content_sha256,
                ),
            ),
            tool_names=(),
            trimming=(),
            allocation=allocation,
        ),
    )


async def _sources(
    session: AsyncSession, run: InvestigationRunRow
) -> tuple[InvestigationRequest, CoordinationPolicyRecord, ModelBudgetPolicy]:
    if (
        run.workflow_version != "v2"
        or run.data_source_mode != "frozen"
        or run.evidence_policy_version != "task-evidence-v1"
    ):
        raise TaskFactConflict(
            "coordination_context_not_supported",
            "首版协调模型上下文只支持使用固定证据策略的 v2 冻结运行",
        )
    incident = await session.get(IncidentRow, run.incident_id)
    policy_row = await session.get(InvestigationCoordinationPolicyRow, run.id)
    if incident is None or policy_row is None or run.model_budget is None:
        raise TaskFactConflict(
            "coordination_context_missing", "协调调用缺少事故、固定策略或模型预算"
        )
    try:
        policy_record = CoordinationPolicyRecord.model_validate(policy_row, from_attributes=True)
        model_policy = ModelBudgetPolicy.model_validate(run.model_budget)
    except ValueError as error:
        raise TaskFactConflict(
            "coordination_context_integrity_violation", "协调上下文来源无法通过校验"
        ) from error
    if model_policy.profile.structured_output_method != "function_calling":
        raise TaskFactConflict(
            "coordination_model_profile_invalid", "协调模型必须使用 function_calling"
        )
    return _request(incident), policy_record, model_policy


async def validate_coordination_context(
    session: AsyncSession,
    run: InvestigationRunRow,
    model_input: CoordinationModelInput,
    manifest: ContextManifest,
) -> None:
    request, policy_record, model_policy = await _sources(session, run)
    prior_task_count = int(
        await session.scalar(
            select(func.count())
            .select_from(AgentTaskRow)
            .join(InvestigationRoundRow, InvestigationRoundRow.id == AgentTaskRow.round_id)
            .where(
                InvestigationRoundRow.investigation_run_id == run.id,
                InvestigationRoundRow.round_number < model_input.round_number,
            )
        )
        or 0
    )
    expected = build_coordination_context(
        request,
        policy_record,
        round_number=model_input.round_number,
        existing_task_count=prior_task_count,
        observation_cutoff_at=model_input.observation_cutoff_at,
        evidence_snapshot_id=model_input.evidence_snapshot_id,
        model_policy=model_policy,
    )
    if expected.model_input != model_input or expected.manifest != manifest:
        raise TaskFactConflict(
            "coordination_context_integrity_violation", "协调输入、材料清单与业务来源不一致"
        )


class CoordinationContextService:
    def __init__(self, factory: async_sessionmaker[AsyncSession], run_id: UUID) -> None:
        self._factory = factory
        self._run_id = run_id

    async def prepare(
        self,
        *,
        observation_cutoff_at: datetime,
        evidence_snapshot_id: str,
    ) -> ContextPacket:
        async with self._factory() as session:
            run = await session.get(InvestigationRunRow, self._run_id)
            if run is None:
                raise InvestigationRunNotFound(self._run_id)
            rounds = list(
                await session.scalars(
                    select(InvestigationRoundRow).where(
                        InvestigationRoundRow.investigation_run_id == self._run_id
                    )
                )
            )
            if rounds:
                raise TaskFactConflict(
                    "coordination_context_not_supported",
                    "首版协调模型上下文只支持创建第一轮计划",
                )
            request, policy_record, model_policy = await _sources(session, run)
            return build_coordination_context(
                request,
                policy_record,
                round_number=1,
                existing_task_count=0,
                observation_cutoff_at=observation_cutoff_at,
                evidence_snapshot_id=evidence_snapshot_id,
                model_policy=model_policy,
            )
