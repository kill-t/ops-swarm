import type {
  ApprovalActionStep,
  PendingApprovalIncident,
  VerificationRule,
} from "./approvals";
import type { OperationExecution } from "./executions";

export type VerificationStatus = "running" | "passed" | "failed";
export type VerificationSampleStatus = "passed" | "failed" | "error";
export type VerificationWorkItemStatus = "pending" | "running" | "completed";

export interface VerificationSample {
  id: string;
  verification_run_id: string;
  idempotency_key: string;
  sequence: number;
  status: VerificationSampleStatus;
  signal: string;
  observed_value: number | null;
  threshold: number;
  http_status: number | null;
  error_code: string | null;
  error_message: string | null;
  observed_at: string;
}

export interface VerificationRun {
  id: string;
  operation_execution_id: string;
  incident_id: string;
  action_step_id: string;
  status: VerificationStatus;
  verifier_name: string;
  incident_version_before: number;
  rule: VerificationRule;
  successful_sample_count: number;
  first_success_at: string | null;
  last_observed_at: string | null;
  error_code: string | null;
  error_message: string | null;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
  samples: VerificationSample[];
}

export interface VerificationWorkItem {
  id: string;
  verification_run_id: string;
  status: VerificationWorkItemStatus;
  next_sample_sequence: number;
  available_at: string;
  locked_by: string | null;
  locked_until: string | null;
  attempt_count: number;
  consecutive_failure_count: number;
  last_error_code: string | null;
  last_error_message: string | null;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
}

export interface VerificationCandidate {
  incident: PendingApprovalIncident;
  operation_execution: OperationExecution;
  action_step: ApprovalActionStep;
  verification_run: VerificationRun | null;
  work_item: VerificationWorkItem | null;
}

export interface VerificationCandidatePage {
  items: VerificationCandidate[];
  total: number;
}

export interface CreateVerificationRunInput {
  expected_incident_version: number;
}

export interface CreateVerificationRunResponse {
  created: boolean;
  verification_run: VerificationRun;
}
