"""只从精确回流新候选重建草案，保留开放假设及调查限制。"""

from uuid import UUID

from opsswarm.investigation.candidate.output import CandidateModelResult
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.reinvestigation_candidate.contracts import (
    ReflowCandidateResult,
    ReflowCandidateSource,
)
from opsswarm.investigation.reinvestigation_candidate.policy import evaluate_candidate
from opsswarm.planning.candidate.policy import build_draft
from opsswarm.planning.reinvestigation_candidate.contracts import ReflowPlanContent, ReflowPlanError


def build_plan(
    job_id: UUID,
    source: ReflowCandidateSource,
    result: ReflowCandidateResult,
    result_sha256: str,
) -> ReflowPlanContent:
    if (
        job_id != source.investigation_run_id
        or result.job_id != job_id
        or result.investigation_run_id != job_id
        or result.source_sha256 != content_hash(source)
        or result.routing_sha256 != source.routing_sha256
        or result.created_at < source.created_at
        or content_hash(result) != result_sha256
    ):
        raise ReflowPlanError("reflow_plan_result_mismatch", "规划必须绑定138完整新结果及精确来源")
    evaluation = result.evaluation
    if evaluation.disposition != "planning_review" or evaluation.step is None:
        raise ReflowPlanError("reflow_plan_not_ready", "回流候选尚无可供规划审查的安全步骤")
    try:
        rebuilt = evaluate_candidate(source, CandidateModelResult(proposal=evaluation.proposal))
    except ValueError as error:
        raise ReflowPlanError("reflow_plan_source_invalid", "新证据闭包不能支持候选") from error
    if rebuilt != evaluation:
        raise ReflowPlanError("reflow_plan_result_mismatch", "候选与新证据确定性重算不一致")
    assert rebuilt.step is not None
    return ReflowPlanContent(
        candidate_job_id=job_id,
        candidate_result_sha256=result_sha256,
        candidate_source_sha256=result.source_sha256,
        routing_sha256=result.routing_sha256,
        review_result_sha256=source.routing.review_result_sha256,
        synthesis_result_sha256=source.routing.synthesis_result_sha256,
        hypothesis=source.review_source.synthesis_result.proposal,
        evidence_ids=evaluation.proposal.evidence_ids,
        preconditions=evaluation.proposal.preconditions,
        candidate_unresolved_questions=evaluation.proposal.unresolved_questions,
        observation_windows=source.routing.observation_windows,
        unresolved_constraints=source.routing.unresolved_constraints,
        plan=build_draft(rebuilt.step),
    )
