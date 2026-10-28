"""账本可独立解析的轻量回流模型结果，避免导入运行层。"""

from typing import Literal

from opsswarm.investigation.coordination.contracts import CoordinationProposal
from opsswarm.investigation.tasks.contracts import TaskModel


class ReflowModelResult(TaskModel):
    kind: Literal["model"] = "model"
    version: Literal["reinvestigation-coordination-result-v1"] = (
        "reinvestigation-coordination-result-v1"
    )
    proposal: CoordinationProposal
