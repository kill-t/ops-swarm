"""重算原候选，确定性生成仍无授权的独立计划草案。"""

from uuid import UUID

from opsswarm.investigation.candidate.contracts import CandidateError, CandidateSource
from opsswarm.investigation.candidate.evaluation import evaluate_candidate
from opsswarm.investigation.candidate.output import CandidateEvaluation, CandidateModelResult
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.planning.candidate.contracts import PlanContent, PlanError
from opsswarm.planning.models import ActionPlan, ActionPlanStatus, ActionStep
from opsswarm.planning.policy import RISK_POLICY_VERSION_V3

PLAN_SUMMARY = "基于已记录候选生成的单步骤处置草案，需另行可信审批。"


def build_draft(step: ActionStep) -> ActionPlan:
    """两种候选来源共用相同草案与风险版本，不提升审批资格。"""
    return ActionPlan(
        version=1,
        status=ActionPlanStatus.DRAFT,
        summary=PLAN_SUMMARY,
        risk_level=step.risk_level,
        requires_human_approval=step.requires_human_approval,
        risk_policy_version=RISK_POLICY_VERSION_V3,
        steps=(step,),
    )


def build_plan(
    job_id: UUID,
    source: CandidateSource,
    result: CandidateEvaluation,
    result_sha256: str,
) -> PlanContent:
    """候选的可行性和引用必须与原证据闭包一致，不能提升来源资格。"""

    if content_hash(result) != result_sha256:
        raise PlanError("candidate_plan_result_mismatch", "候选结果散列不一致")
    if result.disposition != "planning_review" or result.step is None:
        raise PlanError("candidate_plan_not_ready", "候选尚未具备规划审查所需的安全步骤")
    try:
        rebuilt = evaluate_candidate(source, CandidateModelResult(proposal=result.proposal))
    except CandidateError as error:
        raise PlanError("candidate_plan_source_invalid", "候选缺少有效原证据闭包") from error
    if rebuilt != result:
        raise PlanError("candidate_plan_result_mismatch", "候选结果与确定性重算不一致")
    step = rebuilt.step
    assert step is not None
    plan = build_draft(step)
    return PlanContent(
        candidate_job_id=job_id,
        candidate_result_sha256=result_sha256,
        open_hypothesis_id=source.hypothesis.id,
        evidence_ids=result.proposal.evidence_ids,
        preconditions=result.proposal.preconditions,
        unresolved_questions=result.proposal.unresolved_questions,
        plan=plan,
    )
