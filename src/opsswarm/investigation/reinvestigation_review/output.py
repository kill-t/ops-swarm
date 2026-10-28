"""独立审查模型结果的轻量账本包装。"""

from typing import Literal

from opsswarm.investigation.review.contracts import HypothesisReviewProposal
from opsswarm.investigation.tasks.contracts import TaskModel


class ReflowReviewModelResult(TaskModel):
    kind: Literal["model"] = "model"
    version: Literal["reinvestigation-review-result-v1"] = "reinvestigation-review-result-v1"
    proposal: HypothesisReviewProposal
