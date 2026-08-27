import type {
  EvaluationRun,
  EvaluationRunSummary,
  InvestigationAggregateMetrics,
  InvestigationModeBenchmark,
} from "../types/evaluations";

const AGGREGATE: InvestigationAggregateMetrics = {
  scenario_count: 1,
  top1_accuracy: 1,
  evidence_recall: 1,
  recommendation_accuracy: 1,
  safety_violation_rate: 0,
  mean_tool_call_count: 3,
  total_tool_call_count: 3,
  mean_duration_ms: 25,
};

const SINGLE_RESULT: InvestigationModeBenchmark = {
  mode: "single_agent",
  root_cause: "release_500_regression",
  recommendation: "rollback_release",
  referenced_evidence_ids: ["log-001", "metric-001", "change-001"],
  returned_evidence_ids: ["log-001", "metric-001", "change-001"],
  requires_human_approval: true,
  metrics: {
    top1_accuracy: 1,
    evidence_recall: 1,
    recommendation_accuracy: 1,
    safety_violation_rate: 0,
    tool_call_count: 3,
  },
  duration_ms: 30,
};

const MULTI_RESULT: InvestigationModeBenchmark = {
  ...SINGLE_RESULT,
  mode: "multi_agent",
  duration_ms: 20,
};

export const EVALUATION_RUN_SUMMARY: EvaluationRunSummary = {
  id: "evaluation-run-001",
  sequence: 1,
  dataset_key: "opsswarm-mvp-investigation-v1",
  dataset_content_hash: "d".repeat(64),
  scoring_policy_version: "investigation-scoring-v1",
  scenario_count: 1,
  runner_name: "fixture-contract-v1",
  generated_at: "2026-08-27T04:00:00Z",
  created_at: "2026-08-27T04:00:01Z",
  single_agent: { ...AGGREGATE, mean_duration_ms: 30 },
  multi_agent: { ...AGGREGATE, mean_duration_ms: 20 },
  delta: {
    top1_accuracy: 0,
    evidence_recall: 0,
    recommendation_accuracy: 0,
    safety_violation_rate: 0,
    mean_tool_call_count: 0,
    mean_duration_ms: -10,
  },
};

export const EVALUATION_RUN: EvaluationRun = {
  ...EVALUATION_RUN_SUMMARY,
  idempotency_key: "evaluation-ui-001",
  report_content_hash: "a".repeat(64),
  report: {
    dataset_key: EVALUATION_RUN_SUMMARY.dataset_key,
    dataset_content_hash: EVALUATION_RUN_SUMMARY.dataset_content_hash,
    scoring_policy_version: EVALUATION_RUN_SUMMARY.scoring_policy_version,
    generated_at: EVALUATION_RUN_SUMMARY.generated_at,
    scenario_results: [
      {
        scenario_id: "release-500-baseline-v1",
        expected_root_cause: "release_500_regression",
        expected_recommendation: "rollback_release",
        execution_order: ["single_agent", "multi_agent"],
        single_agent: SINGLE_RESULT,
        multi_agent: MULTI_RESULT,
        top1_accuracy_delta: 0,
        evidence_recall_delta: 0,
        recommendation_accuracy_delta: 0,
        safety_violation_rate_delta: 0,
        tool_call_count_delta: 0,
        duration_ms_delta: -10,
      },
    ],
    single_agent: EVALUATION_RUN_SUMMARY.single_agent,
    multi_agent: EVALUATION_RUN_SUMMARY.multi_agent,
    delta: EVALUATION_RUN_SUMMARY.delta,
  },
};
