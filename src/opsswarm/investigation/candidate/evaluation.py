"""检查候选证据闭包，并使用确定性参数模板评估安全可行性。"""

from typing import Protocol

from opsswarm.investigation.candidate.contracts import CandidateError
from opsswarm.investigation.candidate.output import CandidateEvaluation, CandidateModelResult
from opsswarm.investigation.human_wait.contracts import HumanWaitEventRecord
from opsswarm.investigation.models import EvidenceObservation
from opsswarm.investigation.review.contracts import HypothesisReviewProposal
from opsswarm.planning.errors import (
    UnsafeConfigurationRollbackTarget,
    UnsafeDependencyRestoreTarget,
    UnsafeRollbackTarget,
)
from opsswarm.planning.steps import build_candidate_step


class CandidateBasis(Protocol):
    """只要求纯评估所需材料，避免新来源伪装为旧交接来源。"""

    @property
    def service_name(self) -> str: ...

    @property
    def evidence(self) -> tuple[EvidenceObservation, ...]: ...

    @property
    def review(self) -> HypothesisReviewProposal: ...

    @property
    def human_inputs(self) -> tuple[HumanWaitEventRecord, ...]: ...


def validate_proposal(source: CandidateBasis, result: CandidateModelResult) -> None:
    proposal = result.proposal
    evidence_ids = {item.evidence_id for item in source.evidence}
    human_ids = {
        str(identifier)
        for event in source.human_inputs
        for identifier in (event.id, event.authorization_id)
    }
    cited_ids = set(proposal.evidence_ids)
    if cited_ids - evidence_ids or cited_ids & human_ids:
        raise CandidateError("candidate_evidence_invalid", "候选引用必须属于原工具证据闭包")
    if proposal.status == "candidate" and not cited_ids.intersection(
        source.review.accepted_supporting_evidence_ids
    ):
        raise CandidateError(
            "candidate_support_missing", "候选至少需要引用当前审查接受的一条支持证据"
        )


def evaluate_candidate(source: CandidateBasis, result: CandidateModelResult) -> CandidateEvaluation:
    validate_proposal(source, result)
    proposal = result.proposal
    if proposal.status == "unsupported":
        return CandidateEvaluation(
            proposal=proposal,
            disposition="await_human",
            reason_code="candidate_unsupported",
        )
    assert proposal.action_type is not None
    try:
        # 参数也必须有候选显式引用，不能借用未引用的其他冻结材料。
        cited_ids = set(proposal.evidence_ids)
        cited_evidence = tuple(item for item in source.evidence if item.evidence_id in cited_ids)
        step, _ = build_candidate_step(source.service_name, proposal.action_type, cited_evidence)
    except (
        UnsafeRollbackTarget,
        UnsafeConfigurationRollbackTarget,
        UnsafeDependencyRestoreTarget,
    ):
        return CandidateEvaluation(
            proposal=proposal,
            disposition="await_human",
            reason_code="candidate_parameters_unavailable",
        )
    return CandidateEvaluation(
        proposal=proposal,
        disposition="planning_review",
        step=step,
        reason_code="candidate_ready_for_planning_review",
    )
