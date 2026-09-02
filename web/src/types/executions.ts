import type {
  ApprovalActionPlan,
  ApprovalRequest,
  PendingApprovalIncident,
} from "./approvals";

export type ExecutionStatus = "running" | "succeeded" | "failed";
export type JsonValue = string | number | boolean | null | JsonValue[] | JsonObject;

export interface JsonObject {
  [key: string]: JsonValue;
}

export interface OperationExecution {
  id: string;
  idempotency_key: string;
  incident_id: string;
  approval_request_id: string;
  action_plan_id: string;
  action_plan_version: number;
  action_step_id: string;
  status: ExecutionStatus;
  executor_name: string;
  incident_version_before: number;
  attempt_count: number;
  locked_until: string | null;
  result: JsonObject | null;
  error_code: string | null;
  error_message: string | null;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
}

export interface ExecutionCandidate {
  approval_request: ApprovalRequest;
  incident: PendingApprovalIncident;
  action_plan: ApprovalActionPlan;
  active_executions: OperationExecution[];
}

export interface ExecutionCandidatePage {
  items: ExecutionCandidate[];
  total: number;
}

export interface ExecuteOperationInput {
  idempotency_key: string;
  expected_incident_version: number;
}

export interface ExecuteOperationResponse {
  created: boolean;
  execution_performed: boolean;
  operation_execution: OperationExecution;
}
