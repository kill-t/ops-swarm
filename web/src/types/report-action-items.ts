export type ReportActionItemStatus = "open" | "completed";

export interface ReportActionItem {
  id: string;
  report_id: string;
  idempotency_key: string;
  title: string;
  description: string;
  owner_id: string;
  status: ReportActionItemStatus;
  version: number;
  due_at: string;
  created_by_id: string;
  created_at: string;
  completed_by_id: string | null;
  completion_note: string | null;
  completed_at: string | null;
}

export interface ReportActionItemPage {
  items: ReportActionItem[];
  total: number;
}

export interface CreateReportActionItemInput {
  idempotency_key: string;
  title: string;
  description: string;
  owner_id: string;
  due_at: string;
  actor_id: string;
}

export interface CreateReportActionItemResponse {
  created: boolean;
  action_item: ReportActionItem;
}

export interface CompleteReportActionItemInput {
  expected_version: number;
  actor_id: string;
  completion_note: string;
}

export interface CompleteReportActionItemResponse {
  completion_applied: boolean;
  action_item: ReportActionItem;
}

export interface ReportActionItemGovernanceIncident {
  id: string;
  display_number: number;
  reference: string;
  service_name: string;
  title: string;
  severity: "p1" | "p2" | "p3" | "p4";
}

export interface ReportActionItemGovernanceEntry {
  action_item: ReportActionItem;
  incident: ReportActionItemGovernanceIncident;
  overdue: boolean;
}

export interface ReportActionItemGovernancePage {
  items: ReportActionItemGovernanceEntry[];
  total: number;
  as_of: string;
  owner_id: string | null;
  overdue_only: boolean;
}

export interface ReportActionItemGovernanceFilters {
  ownerId?: string;
  overdueOnly?: boolean;
}
