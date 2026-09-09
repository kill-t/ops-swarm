"""工作摘要保留原报告与不可变来源，不推断缺失的问题分类。"""

from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from opsswarm.investigation.context.evidence_contracts import EvidenceCatalogGroup
from opsswarm.investigation.models import SpecialistReport
from opsswarm.investigation.tasks.contracts import TaskAssignment, TaskModel
from opsswarm.investigation.working_summary.hashing import material_hash, serialized_size

SummaryPolicyVersion = Literal["task-working-summary-v1"]
SUMMARY_VERSION: SummaryPolicyVersion = "task-working-summary-v1"


class SummaryCallReference(TaskModel):
    call_id: UUID
    attempt_id: UUID
    sequence: int = Field(ge=1)
    invocation_index: int = Field(ge=0, le=99)
    kind: Literal["model", "tool"]
    operation: str = Field(min_length=1, max_length=32)
    status: Literal["succeeded", "failed", "unknown"]
    error_code: str | None = Field(max_length=100)


class TaskWorkingSummary(TaskModel):
    version: SummaryPolicyVersion = SUMMARY_VERSION
    investigation_run_id: UUID
    task_id: UUID
    attempt_id: UUID
    assignment: TaskAssignment
    report: SpecialistReport
    result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_call_id: UUID
    source_result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_catalog: EvidenceCatalogGroup
    read_evidence_ids: tuple[str, ...] = Field(max_length=20)
    calls: tuple[SummaryCallReference, ...] = Field(min_length=1, max_length=160)

    @model_validator(mode="after")
    def validate_content(self) -> Self:
        ids = {e.evidence_id for e in self.evidence_catalog.entries}
        if (
            self.task_id != self.evidence_catalog.task_id
            or self.assignment.role != self.report.specialist
            or self.assignment.role != self.evidence_catalog.role
            or not set(self.report.evidence_ids) <= set(self.read_evidence_ids) <= ids
            or len(set(self.read_evidence_ids)) != len(self.read_evidence_ids)
            or len({c.call_id for c in self.calls}) != len(self.calls)
            or len({c.sequence for c in self.calls}) != len(self.calls)
            or not any(
                c.call_id == self.source_call_id
                and c.attempt_id == self.attempt_id
                and c.status == "succeeded"
                for c in self.calls
            )
        ):
            raise ValueError("工作摘要归属、已读引用或来源调用不一致")
        if serialized_size(self) > 131072:
            raise ValueError("工作摘要超过 128 KiB，不能静默裁剪")
        return self


class WorkingSummaryRecord(TaskModel):
    task_id: UUID
    investigation_run_id: UUID
    attempt_id: UUID
    version: SummaryPolicyVersion
    summary: TaskWorkingSummary
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: AwareDatetime

    @model_validator(mode="after")
    def validate_record(self) -> Self:
        if (
            self.summary.task_id != self.task_id
            or self.summary.investigation_run_id != self.investigation_run_id
            or self.summary.attempt_id != self.attempt_id
            or self.summary.version != self.version
            or material_hash(self.summary) != self.content_sha256
        ):
            raise ValueError("工作摘要记录归属或散列不一致")
        return self
