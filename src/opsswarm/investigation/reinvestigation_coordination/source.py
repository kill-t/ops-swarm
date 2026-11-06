"""沿已验证候选链冻结回流材料，当前资格与历史输入分别核验。"""

from collections.abc import Callable
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from opsswarm.approval.conditions.queries import raw_content_hash
from opsswarm.incidents.persistence.tables import IncidentRow
from opsswarm.investigation.budget.policy import remaining_time
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.coordination.contracts import CoordinationPolicyRecord
from opsswarm.investigation.coordination.tables import InvestigationCoordinationPolicyRow
from opsswarm.investigation.persistence.lease import normalized_now
from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.investigation.reinvestigation.queries import has_new_alert, read_view
from opsswarm.investigation.reinvestigation.tables import CandidateReinvestigationRow
from opsswarm.investigation.reinvestigation_coordination.context import build_packet
from opsswarm.investigation.reinvestigation_coordination.contracts import (
    ReflowError,
    ReflowMaterial,
    ReflowPacket,
    ReflowSource,
)
from opsswarm.investigation.reinvestigation_coordination.tables import ReflowJobRow
from opsswarm.investigation.settlement.tables import CandidateSettlementRow


async def _json(session: AsyncSession, sql: str, values: dict[str, Any]) -> dict[str, Any]:
    result = await session.scalar(text(sql), values)
    if not isinstance(result, dict):
        raise ReflowError("reflow_integrity_violation", "回流材料缺少已验证的持久正文")
    return result


async def rebuild_source(
    session: AsyncSession,
    run: InvestigationRunRow,
    admission: CandidateReinvestigationRow,
    *,
    created_at: datetime,
    clock: Callable[[], datetime],
    require_applicable: bool = False,
) -> ReflowSource:
    view = await read_view(session, run, admission, clock=clock)
    record = view.record
    if record is None:
        raise ReflowError("reflow_integrity_violation", "回流缺少唯一可信准入")
    settlement = await session.scalar(
        select(CandidateSettlementRow).where(CandidateSettlementRow.investigation_run_id == run.id)
    )
    assert settlement is not None
    values = {
        "run": run.id,
        "candidate": settlement.candidate_job_id,
        "job": settlement.recovery_job_id,
        "version": settlement.verification_version,
        "end": len(record.budget.calls),
    }
    original = await _json(
        session,
        "SELECT source FROM investigation_candidate_evaluation_jobs "
        "WHERE id=:candidate AND investigation_run_id=:run",
        values,
    )
    failed = await session.scalar(
        text("""
      SELECT coalesce(jsonb_agg(jsonb_build_object('id',id,'task_id',task_id,
        'task_attempt_id',task_attempt_id,'operation',operation,'status',status,
        'error_code',error_code,'tool_request',tool_request,'reserved_at',reserved_at,
        'finished_at',finished_at) ORDER BY sequence),'[]'::jsonb)
      FROM investigation_call_reservations WHERE investigation_run_id=:run AND sequence<=:end
        AND status IN ('failed','unknown')
    """),
        values,
    )
    candidate = await _json(
        session,
        "SELECT content FROM investigation_candidate_evaluation_results WHERE job_id=:candidate",
        values,
    )
    plan = await _json(
        session,
        "SELECT to_jsonb(t) FROM candidate_action_plans t "
        "WHERE candidate_job_id=:candidate AND investigation_run_id=:run",
        values,
    )
    conditions = await _json(
        session,
        "SELECT to_jsonb(t) FROM candidate_condition_verifications t "
        "WHERE candidate_job_id=:candidate AND version=:version",
        values,
    )
    execution = await _json(
        session,
        """
      SELECT jsonb_build_object('request',to_jsonb(request),'result',result.content)
      FROM candidate_release_execution_results result
      JOIN candidate_release_execution_requests request
        ON request.id=(result.content->>'request_id')::uuid
      WHERE result.job_id=:job
    """,
        values,
    )
    recovery = await _json(
        session,
        """
      SELECT jsonb_build_object('job',to_jsonb(job),'result',result.content,
        'attempts',(SELECT coalesce(jsonb_agg(to_jsonb(a) ORDER BY a.number),'[]')
          FROM candidate_recovery_attempts a WHERE a.job_id=job.id),
        'samples',(SELECT coalesce(jsonb_agg(to_jsonb(s) ORDER BY a.number,s.sequence),'[]')
          FROM candidate_recovery_samples s JOIN candidate_recovery_attempts a ON a.id=s.attempt_id
          WHERE s.job_id=job.id))
      FROM candidate_recovery_jobs job
      JOIN candidate_recovery_results result ON result.job_id=job.id
      WHERE job.id=:job
    """,
        values,
    )
    convergence = await _json(
        session,
        "SELECT content FROM candidate_incident_convergences WHERE investigation_run_id=:run",
        values,
    )
    policy_row = await session.get(InvestigationCoordinationPolicyRow, run.id)
    if policy_row is None:
        raise ReflowError("reflow_integrity_violation", "原运行协调策略缺失")
    policy = CoordinationPolicyRecord.model_validate(policy_row, from_attributes=True).policy
    rounds = list(
        await session.scalars(
            text(
                "SELECT round_number FROM investigation_rounds "
                "WHERE investigation_run_id=:run ORDER BY round_number"
            ),
            values,
        )
    )
    tasks = await session.scalar(
        text("SELECT count(*) FROM agent_tasks WHERE investigation_run_id=:run"), values
    )
    if rounds != list(range(1, len(rounds) + 1)) or not rounds:
        raise ReflowError("reflow_integrity_violation", "原轮次序列无法重建")
    entries: tuple[tuple[str, str, dict[str, Any]], ...] = (
        (
            "original_investigation",
            str(settlement.candidate_job_id),
            {
                "candidate_source": original,
                "failed_calls": failed,
                **({"input_policy": run.reflow_input_policy} if run.reflow_input_policy else {}),
            },
        ),
        ("current_review", f"{settlement.candidate_job_id}:review", original["review"]),
        ("human_inputs", f"{run.id}:human", {"events": original["human_inputs"]}),
        ("candidate", str(settlement.candidate_job_id), candidate),
        ("action_plan", str(plan["id"]), plan["content"]),
        ("conditions", str(conditions["id"]), conditions["content"]),
        ("execution", str(settlement.recovery_job_id), execution),
        ("recovery", str(settlement.recovery_job_id), recovery),
        ("convergence", str(run.id), convergence),
        (
            "admission",
            str(admission.id),
            {
                "id": str(record.id),
                "content_sha256": record.content_sha256,
                "command": record.command.model_dump(mode="json"),
                "root_run_id": str(record.root_run_id),
                "previous_run_id": str(record.previous_run_id),
                "budget_run_id": str(record.budget_run_id),
            },
        ),
    )
    budget = record.budget
    assert (
        budget.policy is not None
        and budget.model_budget is not None
        and budget.started_at is not None
    )
    source = ReflowSource(
        investigation_run_id=run.id,
        incident_id=run.incident_id,
        admission_sha256=record.content_sha256,
        incident_version=record.incident_version,
        parent_attempt_id=UUID(original["handoff"]["investigation_attempt_id"]),
        coordination_policy=policy,
        next_round_number=rounds[-1] + 1,
        existing_task_count=int(tasks or 0),
        maximum_tasks=max(
            0, min(policy.max_tasks_per_round, policy.max_total_tasks - int(tasks or 0))
        ),
        call_budget=budget.policy,
        model_budget=budget.model_budget,
        started_at=budget.started_at,
        created_at=created_at,
        materials=tuple(
            ReflowMaterial(
                kind=kind,
                reference_id=reference,
                content_sha256=raw_content_hash(body),
                content=body,
            )
            for kind, reference, body in entries
        ),
    )
    if created_at < record.created_at:
        raise ReflowError("reflow_integrity_violation", "回流冻结时刻早于准入")
    if require_applicable:
        await check_current_basis(session, run, source, now=normalized_now(clock))
    return source


async def check_current_basis(
    session: AsyncSession, run: InvestigationRunRow, source: ReflowSource, *, now: datetime
) -> None:
    incident = await session.get(IncidentRow, run.incident_id)
    if (
        run.status != "settled"
        or run.candidate_slot_released_at is not None
        or incident is None
        or incident.status != "investigating"
        or incident.version != source.incident_version
        or await has_new_alert(session, run.id, run.incident_id, source.incident_version)
    ):
        raise ReflowError("reflow_source_changed", "事故、告警或原持槽运行不再适用")
    remaining_time(source.call_budget, source.started_at, now)


async def load_source(
    session: AsyncSession,
    run: InvestigationRunRow,
    admission: CandidateReinvestigationRow,
    *,
    created_at: datetime,
    clock: Callable[[], datetime],
) -> ReflowSource:
    return await rebuild_source(
        session, run, admission, created_at=created_at, clock=clock, require_applicable=True
    )


async def require_current(
    session: AsyncSession,
    run: InvestigationRunRow,
    job: ReflowJobRow,
    *,
    clock: Callable[[], datetime],
) -> tuple[ReflowSource, ReflowPacket]:
    admission = await session.get(CandidateReinvestigationRow, run.id)
    if admission is None or job.source is None or job.packet is None:
        raise ReflowError("reflow_integrity_violation", "回流缺少冻结输入或准入")
    source = await rebuild_source(
        session, run, admission, created_at=job.created_at, clock=clock, require_applicable=True
    )
    packet = build_packet(source)
    if (
        source.model_dump(mode="json") != job.source
        or content_hash(source) != job.source_sha256
        or packet.model_dump(mode="json") != job.packet
        or content_hash(packet) != job.packet_sha256
    ):
        raise ReflowError("reflow_integrity_violation", "回流冻结来源或输入不一致")
    return source, packet
