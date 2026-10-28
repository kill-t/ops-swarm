"""独立专业执行的版本、租约、窗口与可追溯结果契约。"""

from typing import Literal
from uuid import UUID

from pydantic import Field

from opsswarm.investigation.budget.model_contracts import ModelAllocation, ModelInput
from opsswarm.investigation.human_wait.contracts import Hash, HumanWaitModel
from opsswarm.investigation.models import SpecialistReport
from opsswarm.investigation.reinvestigation_execution.output import (
    ExecutionModelResult as ExecutionModelResult,
)
from opsswarm.investigation.tasks.evidence import TaskEvidenceSnapshot
from opsswarm.observation_window import ObservationWindow
from opsswarm.verification.candidate.contracts import UtcTime

POLICY_VERSION = "reinvestigation-task-v1"
EVIDENCE_POLICY_VERSION = "reflow-live-mcp-v1"
COLLECT = "collect_reinvestigation"
ANALYZE = "analyze_reinvestigation"
ROUTE = "/api/v1/investigations/{run_id}/reinvestigation-execution"


class EffectiveWindow(HumanWaitModel):
    policy_version: Literal["reflow-live-mcp-v1"] = "reflow-live-mcp-v1"
    requested_start: UtcTime
    requested_end: UtcTime
    window: ObservationWindow
    adjustments: tuple[str, ...] = Field(max_length=2)


class ExecutionClaim(HumanWaitModel):
    task_id: UUID
    investigation_run_id: UUID
    attempt_id: UUID
    number: int = Field(ge=1, le=3, strict=True)
    lease_token: UUID
    lease_expires_at: UtcTime


class ExecutionManifest(HumanWaitModel):
    version: Literal["reinvestigation-task-context-v1"] = "reinvestigation-task-context-v1"
    policy_version: Literal["reinvestigation-task-v1"] = "reinvestigation-task-v1"
    prompt_version: Literal["reinvestigation-specialist-v1"] = "reinvestigation-specialist-v1"
    dispatch_sha256: Hash
    assignment_sha256: Hash
    source_sha256: Hash
    evidence_sha256: Hash
    input_sha256: Hash
    tool_names: tuple[str, ...] = Field(default=(), max_length=0)
    trimming: tuple[str, ...] = Field(default=(), max_length=0)


class ExecutionPacket(HumanWaitModel):
    model_input: ModelInput
    allocation: ModelAllocation
    manifest: ExecutionManifest


class ExecutionResult(HumanWaitModel):
    investigation_run_id: UUID
    task_id: UUID
    dispatch_sha256: Hash
    evidence_call_id: UUID
    model_call_id: UUID
    evidence_sha256: Hash
    packet_sha256: Hash
    report: SpecialistReport
    created_at: UtcTime


class ExecutionAttempt(HumanWaitModel):
    id: UUID
    number: int
    status: str
    started_at: UtcTime
    finished_at: UtcTime | None
    error_code: str | None


class ExecutionTaskView(HumanWaitModel):
    task_id: UUID
    status: Literal["pending", "queued", "running", "succeeded", "blocked", "failed"]
    effective_window: EffectiveWindow | None
    error_code: str | None
    attempts: tuple[ExecutionAttempt, ...] = Field(max_length=3)
    evidence: TaskEvidenceSnapshot | None
    packet: ExecutionPacket | None
    result: ExecutionResult | None


class ExecutionView(HumanWaitModel):
    investigation_run_id: UUID
    tasks: tuple[ExecutionTaskView, ...] = Field(max_length=3)
