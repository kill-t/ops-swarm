"""轻量专业模型结果包装，避免预算协议形成循环导入。"""

from typing import Literal

from opsswarm.investigation.models import SpecialistReport
from opsswarm.investigation.tasks.contracts import TaskModel


class ExecutionModelResult(TaskModel):
    kind: Literal["model"] = "model"
    version: Literal["reinvestigation-task-model-result-v1"] = (
        "reinvestigation-task-model-result-v1"
    )
    report: SpecialistReport
