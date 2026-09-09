"""上下文输入、材料引用与每次调用的不可变清单。"""

from dataclasses import dataclass
from typing import Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from opsswarm.investigation.budget.model_contracts import ModelAllocation
from opsswarm.investigation.loop.contracts import AnyModelInput
from opsswarm.investigation.models import (
    EvidenceKind,
    EvidenceObservation,
    InvestigationRequest,
    SpecialistReport,
)
from opsswarm.investigation.tasks.contracts import TaskAssignment, TaskModel
from opsswarm.investigation.tasks.evidence import EvidencePolicyVersion, TaskEvidenceRecord
from opsswarm.investigation.tasks.results import TaskResultRecord

ContextPolicyVersion = Literal["task-context-v1", "evidence-read-v1"]
CONTEXT_POLICY_VERSION: ContextPolicyVersion = "task-context-v1"
CONTEXT_PROMPT_VERSION: Literal["fixed-task-context-v1"] = "fixed-task-context-v1"


class TaskContextInput(TaskModel):
    """固定图传递的材料契约，不接受任意聊天历史或授权。"""

    incident: InvestigationRequest
    specialist: EvidenceKind | None = None
    evidence: tuple[EvidenceObservation, ...] = Field(max_length=60)
    reports: tuple[SpecialistReport, ...] = Field(default=(), max_length=3)


class ContextMaterial(TaskModel):
    kind: Literal["incident_input", "task_assignment", "task_evidence", "task_result"]
    material_id: UUID
    version: str = Field(min_length=1, max_length=100)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_ids: tuple[str, ...] = Field(default=(), max_length=20)


class ContextManifest(TaskModel):
    version: ContextPolicyVersion = CONTEXT_POLICY_VERSION
    investigation_run_id: UUID
    task_id: UUID | None
    role: Literal["log", "metric", "change", "synthesis"]
    role_version: Literal["fixed-roles-v1"] = "fixed-roles-v1"
    workflow_version: Literal["v2"] = "v2"
    prompt_version: Literal["fixed-task-context-v1", "evidence-read-v1"] = CONTEXT_PROMPT_VERSION
    evidence_policy_version: EvidencePolicyVersion
    materials: tuple[ContextMaterial, ...] = Field(min_length=3, max_length=10)
    tool_names: tuple[str, ...] = Field(default=(), max_length=1)
    trimming: tuple[str, ...] = Field(default=(), max_length=0)
    allocation: ModelAllocation

    @model_validator(mode="after")
    def validate_strategy(self) -> Self:
        loop = self.version == "evidence-read-v1" and self.role != "synthesis"
        if self.tool_names != (("read_evidence",) if loop else ()) or self.prompt_version != (
            "evidence-read-v1" if loop else "fixed-task-context-v1"
        ):
            raise ValueError("上下文策略与提示、工具集合不匹配")
        return self


class ContextPacket(TaskModel):
    model_input: AnyModelInput
    manifest: ContextManifest


@dataclass(frozen=True)
class ContextTaskMaterial:
    task_id: UUID
    assignment: TaskAssignment
    evidence: TaskEvidenceRecord
    result: TaskResultRecord | None


@dataclass(frozen=True)
class ContextSources:
    run_id: UUID
    task_id: UUID | None
    request: InvestigationRequest
    evidence_policy_version: EvidencePolicyVersion
    tasks: tuple[ContextTaskMaterial, ...]
    context_policy_version: ContextPolicyVersion = CONTEXT_POLICY_VERSION
