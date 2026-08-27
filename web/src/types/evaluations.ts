export type InvestigationMode = "single_agent" | "multi_agent";
export type EvaluationKind = "fixture_contract" | "real_model";

export type RootCause =
  | "release_500_regression"
  | "database_pool_misconfiguration"
  | "redis_unavailable"
  | "undetermined";

export type Recommendation = "rollback_release" | "continue_investigation";

export interface InvestigationAggregateMetrics {
  scenario_count: number;
  top1_accuracy: number;
  evidence_recall: number;
  recommendation_accuracy: number;
  safety_violation_rate: number;
  mean_tool_call_count: number;
  total_tool_call_count: number;
  mean_duration_ms: number;
}

export interface InvestigationAggregateDelta {
  top1_accuracy: number;
  evidence_recall: number;
  recommendation_accuracy: number;
  safety_violation_rate: number;
  mean_tool_call_count: number;
  mean_duration_ms: number;
}

export interface InvestigationModeMetrics {
  top1_accuracy: number;
  evidence_recall: number;
  recommendation_accuracy: number;
  safety_violation_rate: number;
  tool_call_count: number;
}

export interface InvestigationModeBenchmark {
  mode: InvestigationMode;
  root_cause: RootCause;
  recommendation: Recommendation;
  referenced_evidence_ids: string[];
  returned_evidence_ids: string[];
  requires_human_approval: boolean;
  metrics: InvestigationModeMetrics;
  duration_ms: number;
}

export interface InvestigationScenarioBenchmark {
  scenario_id: string;
  expected_root_cause: RootCause;
  expected_recommendation: Recommendation;
  execution_order: [InvestigationMode, InvestigationMode];
  single_agent: InvestigationModeBenchmark;
  multi_agent: InvestigationModeBenchmark;
  top1_accuracy_delta: number;
  evidence_recall_delta: number;
  recommendation_accuracy_delta: number;
  safety_violation_rate_delta: number;
  tool_call_count_delta: number;
  duration_ms_delta: number;
}

export interface InvestigationBenchmarkReport {
  dataset_key: string;
  dataset_content_hash: string;
  scoring_policy_version: string;
  generated_at: string;
  scenario_results: InvestigationScenarioBenchmark[];
  single_agent: InvestigationAggregateMetrics;
  multi_agent: InvestigationAggregateMetrics;
  delta: InvestigationAggregateDelta;
}

export interface EvaluationExperimentSpec {
  experiment_key: string;
  evaluation_kind: EvaluationKind;
  model_provider: string | null;
  model_name: string | null;
  model_parameters_hash: string | null;
  single_agent_prompt_version: string;
  multi_agent_prompt_version: string;
}

export interface EvaluationFailureCounts {
  model_request_error: number;
  tool_error: number;
  timeout: number;
  structured_output_error: number;
  safety_rejection: number;
}

export interface EvaluationModeTelemetry {
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  estimated_cost_usd: string;
  failures: EvaluationFailureCounts;
}

export interface EvaluationRunTelemetry {
  single_agent: EvaluationModeTelemetry;
  multi_agent: EvaluationModeTelemetry;
}

export interface EvaluationRunSummary {
  id: string;
  sequence: number;
  dataset_key: string;
  dataset_content_hash: string;
  scoring_policy_version: string;
  scenario_count: number;
  runner_name: string;
  experiment: EvaluationExperimentSpec;
  repetition_index: number;
  telemetry: EvaluationRunTelemetry;
  generated_at: string;
  created_at: string;
  single_agent: InvestigationAggregateMetrics;
  multi_agent: InvestigationAggregateMetrics;
  delta: InvestigationAggregateDelta;
}

export interface EvaluationRun extends EvaluationRunSummary {
  idempotency_key: string;
  report_content_hash: string;
  report: InvestigationBenchmarkReport;
}

export interface EvaluationRunPage {
  items: EvaluationRunSummary[];
  next_before_sequence: number | null;
}
