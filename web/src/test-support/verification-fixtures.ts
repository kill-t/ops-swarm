import { EXECUTION_CANDIDATE, SUCCEEDED_EXECUTION } from "./execution-fixtures";
import type {
  CreateVerificationRunResponse,
  VerificationCandidate,
  VerificationRun,
  VerificationWorkItem,
} from "../types/verifications";

export const VERIFICATION_RUN: VerificationRun = {
  id: "verification-001",
  operation_execution_id: SUCCEEDED_EXECUTION.id,
  incident_id: SUCCEEDED_EXECUTION.incident_id,
  action_step_id: SUCCEEDED_EXECUTION.action_step_id,
  status: "running",
  verifier_name: "prometheus_order_http_5xx_ratio_v1",
  incident_version_before: 5,
  rule: {
    signal: "http_5xx_ratio",
    operator: "less_than",
    threshold: 0.01,
    observation_window_seconds: 60,
    required_successful_samples: 3,
  },
  successful_sample_count: 1,
  first_success_at: "2099-09-01T09:07:00Z",
  last_observed_at: "2099-09-01T09:07:00Z",
  error_code: null,
  error_message: null,
  created_at: "2099-09-01T09:06:02Z",
  updated_at: "2099-09-01T09:07:00Z",
  completed_at: null,
  samples: [
    {
      id: "sample-001",
      verification_run_id: "verification-001",
      idempotency_key: "verification-scheduler-work-001-1",
      sequence: 1,
      status: "passed",
      signal: "http_5xx_ratio",
      observed_value: 0,
      threshold: 0.01,
      http_status: 200,
      error_code: null,
      error_message: null,
      observed_at: "2099-09-01T09:07:00Z",
    },
  ],
};

export const VERIFICATION_WORK_ITEM: VerificationWorkItem = {
  id: "verification-001",
  verification_run_id: "verification-001",
  status: "pending",
  next_sample_sequence: 2,
  available_at: "2099-09-01T09:07:30Z",
  locked_by: null,
  locked_until: null,
  attempt_count: 1,
  consecutive_failure_count: 0,
  last_error_code: null,
  last_error_message: null,
  created_at: "2099-09-01T09:06:02Z",
  updated_at: "2099-09-01T09:07:00Z",
  completed_at: null,
};

export const READY_VERIFICATION_CANDIDATE: VerificationCandidate = {
  incident: {
    ...EXECUTION_CANDIDATE.incident,
    status: "verifying",
    version: 5,
    updated_at: "2099-09-01T09:06:01Z",
  },
  operation_execution: SUCCEEDED_EXECUTION,
  action_step: EXECUTION_CANDIDATE.action_plan.steps[0],
  verification_run: null,
  work_item: null,
};

export const RUNNING_VERIFICATION_CANDIDATE: VerificationCandidate = {
  ...READY_VERIFICATION_CANDIDATE,
  verification_run: VERIFICATION_RUN,
  work_item: VERIFICATION_WORK_ITEM,
};

export const CREATED_VERIFICATION_RESPONSE: CreateVerificationRunResponse = {
  created: true,
  verification_run: {
    ...VERIFICATION_RUN,
    successful_sample_count: 0,
    first_success_at: null,
    last_observed_at: null,
    updated_at: "2099-09-01T09:06:02Z",
    samples: [],
  },
};
