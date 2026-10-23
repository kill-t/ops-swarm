"""按精确来源重建候选材料，历史事实与当前适用性分开。"""

import json
from collections.abc import Callable
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from opsswarm.incidents.persistence.tables import IncidentRow
from opsswarm.investigation.budget.contracts import CallBudgetPolicy
from opsswarm.investigation.budget.model_contracts import ModelBudgetPolicy
from opsswarm.investigation.candidate.contracts import CandidateError, CandidateSource, SourceKind
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.handoff.persistence import validated_handoff
from opsswarm.investigation.handoff.tables import InvestigationTerminalHandoffRow
from opsswarm.investigation.human_prepare.contracts import PreparationSource
from opsswarm.investigation.human_prepare.tables import HumanPreparationJobRow
from opsswarm.investigation.human_review.tables import HumanReviewJobRow, HumanReviewResultRow
from opsswarm.investigation.human_routing.contracts import HumanRoutingRecord
from opsswarm.investigation.human_routing.queries import read_route
from opsswarm.investigation.human_routing.tables import HumanReviewRouteRow
from opsswarm.investigation.human_wait.contracts import HumanWaitEventRecord
from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.investigation.review.contracts import HypothesisReviewProposal, ReviewModelResult
from opsswarm.investigation.review.persistence import validated_review_call
from opsswarm.investigation.review.tables import InvestigationHypothesisReviewRow
from opsswarm.investigation.review_routing.contracts import ReviewRoutingRecord
from opsswarm.investigation.review_routing.tables import InvestigationHypothesisReviewRouteRow
from opsswarm.investigation.synthesis.contracts import OpenHypothesisRecord


async def load_source(
    session: AsyncSession,
    run: InvestigationRunRow,
    kind: SourceKind,
    source_id: UUID,
    *,
    clock: Callable[[], datetime],
) -> tuple[CandidateSource, bool]:
    incident = await session.get(IncidentRow, run.incident_id)
    if (
        incident is None
        or run.call_budget is None
        or run.model_budget is None
        or run.started_at is None
    ):
        raise CandidateError("candidate_source_invalid", "候选来源缺少事故或原固定预算")
    human_route: HumanRoutingRecord | None = None
    human_inputs: tuple[HumanWaitEventRecord, ...] = ()
    if kind == "terminal_handoff":
        row = await session.scalar(
            select(InvestigationTerminalHandoffRow).where(
                InvestigationTerminalHandoffRow.id == source_id,
                InvestigationTerminalHandoffRow.investigation_run_id == run.id,
            )
        )
        if row is None:
            raise CandidateError("candidate_source_not_found", "候选交接不存在", 404)
        handoff = await validated_handoff(session, row)
        if handoff.destination != "candidate_evaluation":
            raise CandidateError("candidate_source_not_eligible", "原交接没有候选评估意图")
        routing = await session.get(
            InvestigationHypothesisReviewRouteRow, handoff.source_review_route_id
        )
        assert routing is not None
        original = ReviewRoutingRecord.model_validate(routing, from_attributes=True)
        reviewed = await session.get(
            InvestigationHypothesisReviewRow, original.hypothesis_review_id
        )
        assert reviewed is not None
        prior_input, _, _ = await validated_review_call(
            session,
            run,
            reviewed.source_call_id,
            expected_round_number=original.decision.round_number,
        )
        review = HypothesisReviewProposal.model_validate(
            {k: v for k, v in reviewed.review.items() if k not in {"version", "status"}}
        )
        reference_hash = content_hash(handoff)
        current = run.status == "await_candidate"
    else:
        row_human = await session.scalar(
            select(HumanReviewRouteRow).where(
                HumanReviewRouteRow.id == source_id,
                HumanReviewRouteRow.investigation_run_id == run.id,
            )
        )
        if row_human is None:
            raise CandidateError("candidate_source_not_found", "重评候选路由不存在", 404)
        job = await session.get(HumanReviewJobRow, row_human.job_id)
        assert job is not None
        view = await read_route(session, job, run, clock=clock)
        human_route = view.record
        if human_route is None or human_route.decision.route != "candidate_evaluation":
            raise CandidateError("candidate_source_not_eligible", "重评没有候选评估意图")
        # read_route 已在本事务完整核验不可变准备与重评，直接解码同一正文，避免重复遍历。
        preparation = await session.get(HumanPreparationJobRow, job.preparation_job_id)
        result = await session.get(HumanReviewResultRow, job.id)
        assert preparation is not None and result is not None
        supplied = PreparationSource.model_validate(preparation.source)
        handoff, original, prior_input = supplied.handoff, supplied.route, supplied.prior_input
        human_inputs = supplied.wait.events
        review = ReviewModelResult.model_validate(result.content).proposal
        if any(
            e.command.action == "supplement"
            and any(i.kind == "constraint" for i in e.command.items)
            for e in human_inputs
        ):
            raise CandidateError("candidate_constraints_unresolved", "人工约束尚未解除")
        reference_hash = content_hash(human_route)
        current = view.availability == "current" and run.status == "await_human"
    material = json.loads(
        next(message.content for message in prior_input.messages if message.role == "user")
    )
    source = CandidateSource(
        source_kind=kind,
        source_id=source_id,
        source_sha256=reference_hash,
        service_name=incident.service_name,
        handoff=handoff,
        original_route=original,
        human_route=human_route,
        human_inputs=human_inputs,
        hypothesis=OpenHypothesisRecord.model_validate(material["open_hypothesis"]),
        review=review,
        prior_input=prior_input,
        evidence=material["evidence"],
        call_budget=CallBudgetPolicy.model_validate(run.call_budget),
        model_budget=ModelBudgetPolicy.model_validate(run.model_budget),
        started_at=run.started_at,
    )
    return source, current and incident.status == "investigating"
