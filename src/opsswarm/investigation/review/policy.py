"""独立审查的证据闭包与开放假设来源约束。"""

from collections.abc import Collection

from opsswarm.investigation.review.contracts import HypothesisReviewProposal
from opsswarm.investigation.synthesis.contracts import OpenHypothesis
from opsswarm.investigation.tasks.errors import TaskFactConflict


def validate_review_evidence(
    proposal: HypothesisReviewProposal,
    hypothesis: OpenHypothesis,
    allowed_evidence_ids: Collection[str],
) -> None:
    """审查只能接受原假设的支持引用，并在本轮材料内标注反证。"""

    accepted = set(proposal.accepted_supporting_evidence_ids)
    if not accepted <= set(hypothesis.supporting_evidence_ids):
        raise TaskFactConflict(
            "hypothesis_review_support_mismatch",
            "独立审查接受了开放假设未引用的支持证据",
        )
    referenced = accepted | set(proposal.counter_evidence_ids)
    if not referenced <= set(allowed_evidence_ids):
        raise TaskFactConflict(
            "hypothesis_review_evidence_mismatch",
            "独立审查引用了当前轮次已选任务之外的证据",
        )
