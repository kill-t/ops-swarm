export type InvestigationMode = "single_agent" | "multi_agent";
export type InvestigationDataSourceMode = "frozen" | "live";
export type InvestigationRunStatus = "queued" | "running" | "succeeded" | "failed";
export type HypothesisStatus = "supported" | "contested" | "insufficient_evidence";
export type RootCause =
  | "release_500_regression"
  | "database_pool_misconfiguration"
  | "redis_unavailable"
  | "undetermined";
export type ActionPlanStatus =
  | "draft"
  | "awaiting_approval"
  | "approved"
  | "rejected"
  | "expired";
export type RiskLevel = "low" | "medium" | "high" | "critical";
export type ApprovalRequestStatus = "pending" | "approved" | "rejected" | "expired";
export type ExecutionStatus = "running" | "succeeded" | "failed";
export type VerificationStatus = "running" | "passed" | "failed";

export interface WorkroomInvestigationSummary {
  id: string;
  mode: InvestigationMode;
  data_source_mode: InvestigationDataSourceMode;
  status: InvestigationRunStatus;
  prompt_version: string;
  model_name: string | null;
  error_code: string | null;
  error_message: string | null;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
  updated_at: string;
}

export interface WorkroomHypothesisSummary {
  id: string;
  root_cause: RootCause;
  statement: string;
  confidence: number;
  status: HypothesisStatus;
  challenge_summary: string;
  rule_version: string;
}

export interface WorkroomEvidenceBoardSummary {
  evidence_count: number;
  specialist_report_count: number;
  primary_hypothesis: WorkroomHypothesisSummary | null;
}

export interface WorkroomActionPlanSummary {
  id: string;
  version: number;
  status: ActionPlanStatus;
  summary: string;
  risk_level: RiskLevel;
  requires_human_approval: boolean;
  step_count: number;
  created_at: string;
}

export interface WorkroomApprovalSummary {
  id: string;
  status: ApprovalRequestStatus;
  requested_by_id: string;
  request_reason: string;
  expires_at: string;
  created_at: string;
  decided_by_id: string | null;
  decision_reason: string | null;
  decided_at: string | null;
}

export interface WorkroomExecutionSummary {
  id: string;
  action_step_id: string;
  status: ExecutionStatus;
  executor_name: string;
  attempt_count: number;
  error_code: string | null;
  error_message: string | null;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
}

export interface WorkroomVerificationSummary {
  id: string;
  status: VerificationStatus;
  signal: string;
  threshold: number;
  observation_window_seconds: number;
  required_successful_samples: number;
  successful_sample_count: number;
  sample_count: number;
  failure_code: string | null;
  failure_message: string | null;
  last_observed_at: string | null;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
}

export interface IncidentWorkroomSnapshot {
  incident_id: string;
  incident_version: number;
  investigation: WorkroomInvestigationSummary | null;
  evidence_board: WorkroomEvidenceBoardSummary | null;
  action_plan: WorkroomActionPlanSummary | null;
  approval_request: WorkroomApprovalSummary | null;
  operation_execution: WorkroomExecutionSummary | null;
  verification_run: WorkroomVerificationSummary | null;
}
