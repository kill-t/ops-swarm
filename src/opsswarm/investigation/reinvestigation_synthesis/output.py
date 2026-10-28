"""独立综合模型结果的轻量账本包装。"""

from typing import Literal

from opsswarm.investigation.synthesis.contracts import OpenHypothesisProposal
from opsswarm.investigation.tasks.contracts import TaskModel


class SynthesisModelResult(TaskModel):
    kind: Literal["model"] = "model"
    version: Literal["reinvestigation-synthesis-result-v1"] = "reinvestigation-synthesis-result-v1"
    proposal: OpenHypothesisProposal
