import type { ActorType, IncidentSeverity, IncidentStatus } from "./incidents";
import type { ActionPlanStatus, RiskLevel } from "./workroom";

export type ApprovalDecision = "approve" | "reject";
export type ApprovalRequestStatus = "pending" | "approved" | "rejected" | "expired";
export type ActionType =
  | "rollback_release"
  | "rollback_configuration"
  | "restore_dependency";

export interface ApprovalRequest {
  id: string;
  incident_id: string;
  action_plan_id: string;
  action_plan_version: number;
  plan_content_hash: string;
  status: ApprovalRequestStatus;
  requested_by_type: ActorType;
  requested_by_id: string;
  request_reason: string;
  expires_at: string;
  created_at: string;
  decided_by_type: ActorType | null;
  decided_by_id: string | null;
  decision_reason: string | null;
  decided_at: string | null;
}

export interface PendingApprovalIncident {
  id: string;
  display_number: number;
  reference: string;
  service_name: string;
  title: string;
  severity: IncidentSeverity;
  status: IncidentStatus;
  version: number;
  updated_at: string;
}

interface VerificationRule {
  signal: string;
  operator: "less_than";
  threshold: number;
  observation_window_seconds: number;
  required_successful_samples: number;
}

interface ApprovalActionStepBase {
  id: string;
  step_order: number;
  target_type: "service" | "dependency";
  target_name: string;
  risk_level: RiskLevel;
  requires_human_approval: boolean;
  risk_rule_code: string;
  risk_rationale: string;
  verification: VerificationRule;
  created_at: string;
}

export interface RollbackReleaseStep extends ApprovalActionStepBase {
  action_type: "rollback_release";
  parameters: {
    current_version: string;
    target_version: string;
  };
}

export interface RollbackConfigurationStep extends ApprovalActionStepBase {
  action_type: "rollback_configuration";
  parameters: {
    key: "database_pool_size";
    current_value: number;
    target_value: number;
  };
}

export interface RestoreDependencyStep extends ApprovalActionStepBase {
  action_type: "restore_dependency";
  parameters: {
    dependency: "redis";
    expected_available: boolean;
    target_available: boolean;
  };
}

export type ApprovalActionStep =
  | RollbackReleaseStep
  | RollbackConfigurationStep
  | RestoreDependencyStep;

export interface ApprovalActionPlan {
  id: string;
  investigation_run_id: string;
  hypothesis_id: string;
  version: number;
  status: ActionPlanStatus;
  summary: string;
  risk_level: RiskLevel;
  requires_human_approval: boolean;
  risk_policy_version: string;
  steps: ApprovalActionStep[];
  created_at: string;
}

export interface PendingApproval {
  approval_request: ApprovalRequest;
  incident: PendingApprovalIncident;
  action_plan: ApprovalActionPlan;
}

export interface PendingApprovalPage {
  items: PendingApproval[];
  total: number;
}

export interface ApprovalDecisionInput {
  expected_incident_version: number;
  actor_id: string;
  reason: string;
}

export interface ApprovalDecisionResponse {
  decision_applied: boolean;
  approval_request: ApprovalRequest;
}
