import { PENDING_APPROVAL } from "./approval-fixtures";
import type {
  ExecuteOperationResponse,
  ExecutionCandidate,
  OperationExecution,
} from "../types/executions";

export const EXECUTION_CANDIDATE: ExecutionCandidate = {
  approval_request: {
    ...PENDING_APPROVAL.approval_request,
    status: "approved",
    decided_by_type: "human",
    decided_by_id: "engineer-002",
    decision_reason: "确认回滚版本、风险和恢复判据。",
    decided_at: "2099-09-01T09:05:00Z",
  },
  incident: {
    ...PENDING_APPROVAL.incident,
    status: "mitigating",
    version: 4,
    updated_at: "2099-09-01T09:05:00Z",
  },
  action_plan: {
    ...PENDING_APPROVAL.action_plan,
    status: "approved",
  },
  active_executions: [],
};

export const SUCCEEDED_EXECUTION: OperationExecution = {
  id: "execution-001",
  idempotency_key: "ui-exec:approval-001:step-001",
  incident_id: "incident-001",
  approval_request_id: "approval-001",
  action_plan_id: "plan-001",
  action_plan_version: 1,
  action_step_id: "step-001",
  status: "succeeded",
  executor_name: "local_fault_lab_release_500_v1",
  incident_version_before: 4,
  attempt_count: 1,
  locked_until: null,
  result: { fault: "release_500", enabled: false },
  error_code: null,
  error_message: null,
  created_at: "2099-09-01T09:06:00Z",
  updated_at: "2099-09-01T09:06:01Z",
  completed_at: "2099-09-01T09:06:01Z",
};

export const SUCCEEDED_EXECUTION_RESPONSE: ExecuteOperationResponse = {
  created: true,
  execution_performed: true,
  operation_execution: SUCCEEDED_EXECUTION,
};
