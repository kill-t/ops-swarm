export type InvestigationAttemptStatus = "running" | "succeeded" | "failed" | "lease_expired";

export interface InvestigationModelPricing {
  input_cost_per_million_usd: string;
  output_cost_per_million_usd: string;
}

export interface InvestigationModelUsage {
  model_request_count: number;
  usage_complete: boolean;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  estimated_cost_usd: string | null;
}

export interface InvestigationAttemptError {
  error_code: string;
  message: string;
}

export interface InvestigationAttempt {
  id: string;
  work_item_id: string;
  attempt_number: number;
  status: InvestigationAttemptStatus;
  worker_id: string;
  model_name: string | null;
  pricing: InvestigationModelPricing | null;
  usage: InvestigationModelUsage;
  started_at: string;
  lease_expires_at: string;
  finished_at: string | null;
  duration_ms: number | null;
  will_retry: boolean | null;
  error: InvestigationAttemptError | null;
}

export interface InvestigationAttemptList {
  investigation_run_id: string;
  items: InvestigationAttempt[];
}
