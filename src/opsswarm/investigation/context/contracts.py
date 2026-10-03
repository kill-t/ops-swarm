"""上下文输入、材料引用与每次调用的不可变清单。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Self
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

if TYPE_CHECKING:
    from opsswarm.investigation.synthesis.contracts import OpenHypothesisRecord
    from opsswarm.investigation.working_summary.contracts import WorkingSummaryRecord

ContextPolicyVersion = Literal["task-context-v1", "evidence-read-v1", "summary-context-v1"]
CONTEXT_POLICY_VERSION: ContextPolicyVersion = "task-context-v1"
CONTEXT_PROMPT_VERSION: Literal["fixed-task-context-v1"] = "fixed-task-context-v1"


class TaskContextInput(TaskModel):
    """固定图传递的材料契约，不接受任意聊天历史或授权。"""

    incident: InvestigationRequest
    specialist: EvidenceKind | None = None
    evidence: tuple[EvidenceObservation, ...] = Field(max_length=60)
    reports: tuple[SpecialistReport, ...] = Field(default=(), max_length=3)


class ContextMaterial(TaskModel):
    kind: Literal[
        "incident_input",
        "coordination_policy",
        "task_assignment",
        "task_evidence",
        "task_result",
        "task_working_summary",
        "open_hypothesis",
    ]
    material_id: UUID
    version: str = Field(min_length=1, max_length=100)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_ids: tuple[str, ...] = Field(default=(), max_length=20)


class ContextManifest(TaskModel):
    version: ContextPolicyVersion | Literal["coordination-context-v1"] = CONTEXT_POLICY_VERSION
    investigation_run_id: UUID
    task_id: UUID | None
    role: Literal["log", "metric", "change", "synthesis", "coordinator", "reviewer"]
    role_version: Literal[
        "fixed-roles-v1",
        "dynamic-coordinator-v1",
        "dynamic-synthesis-v1",
        "dynamic-follow-up-synthesis-v1",
        "dynamic-third-round-synthesis-v1",
        "independent-review-v1",
        "independent-follow-up-review-v1",
    ] = "fixed-roles-v1"
    workflow_version: Literal["v2", "v3"] = "v2"
    prompt_version: Literal[
        "fixed-task-context-v1",
        "evidence-read-v1",
        "summary-context-v1",
        "coordination-plan-v1",
        "open-hypothesis-synthesis-v1",
        "follow-up-open-hypothesis-synthesis-v1",
        "third-round-open-hypothesis-synthesis-v1",
        "open-hypothesis-review-v1",
        "follow-up-open-hypothesis-review-v1",
    ] = CONTEXT_PROMPT_VERSION
    evidence_policy_version: EvidencePolicyVersion
    materials: tuple[ContextMaterial, ...] = Field(min_length=2, max_length=11)
    tool_names: tuple[str, ...] = Field(default=(), max_length=1)
    trimming: tuple[str, ...] = Field(default=(), max_length=0)
    allocation: ModelAllocation

    @model_validator(mode="after")
    def validate_strategy(self) -> Self:
        if self.role == "coordinator":
            if (
                self.version != "coordination-context-v1"
                or self.task_id is not None
                or self.role_version != "dynamic-coordinator-v1"
                or self.prompt_version != "coordination-plan-v1"
                or self.evidence_policy_version != "task-evidence-v1"
                or self.tool_names
                or self.trimming
                or tuple(item.kind for item in self.materials)
                != ("incident_input", "coordination_policy")
            ):
                raise ValueError("协调上下文策略、材料或权限不匹配")
            return self
        if self.role == "synthesis" and self.workflow_version == "v3":
            synthesis_version = (
                self.role_version,
                self.prompt_version,
            ) in (
                ("dynamic-synthesis-v1", "open-hypothesis-synthesis-v1"),
                (
                    "dynamic-follow-up-synthesis-v1",
                    "follow-up-open-hypothesis-synthesis-v1",
                ),
                (
                    "dynamic-third-round-synthesis-v1",
                    "third-round-open-hypothesis-synthesis-v1",
                ),
            )
            if (
                self.version != "summary-context-v1"
                or self.task_id is not None
                or not synthesis_version
                or self.tool_names
                or self.trimming
                or len(self.materials) < 4
            ):
                raise ValueError("动态综合上下文策略、材料或权限不匹配")
            return self
        if self.role == "reviewer":
            review_version = (
                self.role_version,
                self.prompt_version,
            ) in (
                ("independent-review-v1", "open-hypothesis-review-v1"),
                (
                    "independent-follow-up-review-v1",
                    "follow-up-open-hypothesis-review-v1",
                ),
            )
            if (
                self.version != "summary-context-v1"
                or self.workflow_version != "v3"
                or self.task_id is not None
                or not review_version
                or self.tool_names
                or self.trimming
                or len(self.materials) < 5
                or tuple(item.kind for item in self.materials).count("open_hypothesis") != 1
                or self.materials[-1].kind != "open_hypothesis"
            ):
                raise ValueError("独立审查上下文策略、材料或权限不匹配")
            return self
        if (
            self.version == "coordination-context-v1"
            or self.role_version != "fixed-roles-v1"
            or len(self.materials) < 3
        ):
            raise ValueError("任务上下文策略、角色版本或材料不匹配")
        loop = (
            self.version in ("evidence-read-v1", "summary-context-v1") and self.role != "synthesis"
        )
        prompt = (
            "evidence-read-v1"
            if loop
            else (
                "summary-context-v1"
                if self.version == "summary-context-v1"
                else "fixed-task-context-v1"
            )
        )
        if self.tool_names != (("read_evidence",) if loop else ()) or self.prompt_version != prompt:
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
    working_summary: WorkingSummaryRecord | None = None


@dataclass(frozen=True)
class ContextSources:
    run_id: UUID
    task_id: UUID | None
    request: InvestigationRequest
    evidence_policy_version: EvidencePolicyVersion
    tasks: tuple[ContextTaskMaterial, ...]
    workflow_version: Literal["v2", "v3"] = "v2"
    context_policy_version: ContextPolicyVersion = CONTEXT_POLICY_VERSION
    round_number: int | None = None
    open_hypothesis: OpenHypothesisRecord | None = None
    source_synthesis_manifest: ContextManifest | None = None
