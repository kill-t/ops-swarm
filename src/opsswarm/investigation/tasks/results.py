"""专业结果快照的结构、引用与体积约束。"""

import json
from datetime import datetime
from typing import Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from opsswarm.investigation.models import EvidenceObservation, SpecialistReport
from opsswarm.investigation.tasks.contracts import TaskModel


class SpecialistTaskResult(TaskModel):
    """一份已校验的专业报告及实际查询所得材料，不代表最终根因。"""

    evidence_snapshot_id: str = Field(min_length=1, max_length=100)
    data_source_mode: Literal["frozen", "live"] = "frozen"
    report: SpecialistReport
    evidence: tuple[EvidenceObservation, ...] = Field(max_length=20)
    tool_call_count: Literal[1] = 1

    @model_validator(mode="after")
    def validate_material(self) -> Self:
        ids = {item.evidence_id for item in self.evidence}
        if len(ids) != len(self.evidence):
            raise ValueError("专业结果的证据 ID 不能重复")
        if any(item.kind is not self.report.specialist for item in self.evidence):
            raise ValueError("专业结果包含其他角色的证据")
        if not set(self.report.evidence_ids) <= ids:
            raise ValueError("专业报告引用了未返回的证据")
        if len(json.dumps(self.model_dump(mode="json"), ensure_ascii=False).encode()) > 131072:
            raise ValueError("专业结果不能超过 128 KiB")
        return self


class TaskResultRecord(TaskModel):
    task_id: UUID
    investigation_run_id: UUID
    attempt_id: UUID
    result: SpecialistTaskResult
    created_at: datetime
