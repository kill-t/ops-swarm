"""开放假设引用范围的确定性校验。"""

from collections.abc import Collection

from opsswarm.investigation.synthesis.contracts import OpenHypothesisProposal
from opsswarm.investigation.tasks.errors import TaskFactConflict


def validate_proposal_evidence(
    proposal: OpenHypothesisProposal, allowed_evidence_ids: Collection[str]
) -> None:
    """待审查关系也只能引用本轮实际提供给模型的证据。"""

    referenced = set(proposal.supporting_evidence_ids) | set(proposal.counter_evidence_ids)
    unknown = referenced - set(allowed_evidence_ids)
    if unknown:
        raise TaskFactConflict(
            "open_hypothesis_evidence_mismatch",
            "开放假设引用了首轮已选任务之外的证据",
        )
