"""候选规划命令、不可授权内容与历史读取契约。"""

from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from opsswarm.investigation.candidate.output import CandidateItem, EvidenceId
from opsswarm.investigation.human_routing.contracts import UnresolvedConstraint
from opsswarm.investigation.human_wait.contracts import Hash, HumanWaitModel
from opsswarm.investigation.reinvestigation_routing.contracts import RoutingWindow
from opsswarm.investigation.synthesis.contracts import OpenHypothesisProposal
from opsswarm.planning.models import ActionPlan, ActionPlanStatus
from opsswarm.planning.policy import RISK_POLICY_VERSION_V3, validate_action_plan_policy

POLICY_VERSION: Literal["reinvestigation-candidate-plan-v1"] = "reinvestigation-candidate-plan-v1"


class ReflowPlanCommand(HumanWaitModel):
    """客户端只能选择已有候选，不可覆盖动作或授权。"""

    action: Literal["create_plan"] = "create_plan"
    candidate_result_sha256: Hash
    routing_sha256: Hash
    expected_incident_version: int = Field(ge=1, strict=True)
    idempotency_key: str = Field(
        min_length=8, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]+$"
    )
    reason: str = Field(min_length=1, max_length=1000)


class ReflowPlanContent(HumanWaitModel):
    """保留开放假设与未决条件的单步骤草案，不构成审批或执行资格。"""

    version: Literal["reinvestigation-candidate-plan-content-v1"] = (
        "reinvestigation-candidate-plan-content-v1"
    )
    candidate_job_id: UUID
    candidate_result_sha256: Hash
    candidate_source_sha256: Hash
    routing_sha256: Hash
    review_result_sha256: Hash
    synthesis_result_sha256: Hash
    hypothesis: OpenHypothesisProposal
    evidence_ids: tuple[EvidenceId, ...] = Field(min_length=1, max_length=100)
    preconditions: tuple[CandidateItem, ...] = Field(min_length=1, max_length=10)
    candidate_unresolved_questions: tuple[CandidateItem, ...] = Field(max_length=10)
    observation_windows: tuple[RoutingWindow, ...] = Field(min_length=1, max_length=3)
    unresolved_constraints: tuple[UnresolvedConstraint, ...] = Field(max_length=75)
    plan: ActionPlan
    approval_eligible: Literal[False] = False
    execution_eligible: Literal[False] = False

    @model_validator(mode="after")
    def validate_draft(self) -> Self:
        if (
            self.plan.version != 1
            or self.plan.status is not ActionPlanStatus.DRAFT
            or len(self.plan.steps) != 1
            or self.plan.risk_policy_version != RISK_POLICY_VERSION_V3
        ):
            raise ValueError("候选计划只能是版本1、单步骤、action-risk-v3的草案")
        validate_action_plan_policy(self.plan)
        return self


class ReflowPlanRecord(HumanWaitModel):
    """不可变计划行的完整事实，不投影第二套当前事故状态。"""

    id: UUID
    investigation_run_id: UUID
    candidate_job_id: UUID
    candidate_result_sha256: Hash
    routing_sha256: Hash
    incident_version: int = Field(ge=1, strict=True)
    policy_version: Literal["reinvestigation-candidate-plan-v1"]
    requested_by_id: str = Field(min_length=1, max_length=100)
    authorization_id: UUID
    idempotency_key: str = Field(min_length=8, max_length=100)
    command_sha256: Hash
    content: ReflowPlanContent
    content_sha256: Hash
    credential_expires_at: AwareDatetime
    created_at: AwareDatetime


class ReflowPlanView(HumanWaitModel):
    record: ReflowPlanRecord | None
    availability: Literal["pending", "current", "stale"]


class ReflowPlanSubmissionResult(HumanWaitModel):
    applied: bool
    plan: ReflowPlanView


class ReflowPlanAuditRecord(HumanWaitModel):
    cursor: int
    authorization_id: UUID
    investigation_run_id: UUID
    candidate_job_id: UUID
    subject_id: str
    command: ReflowPlanCommand
    command_sha256: Hash
    outcome: Literal["applied", "replayed", "error"]
    http_status: Literal[200, 201, 404, 409]
    plan_id: UUID | None
    error_code: str | None
    error_message: str | None
    credential_expires_at: AwareDatetime
    created_at: AwareDatetime


class ReflowPlanAuditPage(HumanWaitModel):
    items: tuple[ReflowPlanAuditRecord, ...] = Field(max_length=100)
    next_cursor: int | None


class ReflowPlanError(ValueError):
    def __init__(self, code: str, message: str, status_code: int = 409) -> None:
        super().__init__(message)
        self.code, self.status_code = code, status_code
