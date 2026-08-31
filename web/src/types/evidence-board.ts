import type { InvestigationRunStatus } from "./workroom";

export type JsonValue =
  | string
  | number
  | boolean
  | null
  | JsonValue[]
  | { [key: string]: JsonValue };

export type EvidenceKind = "log" | "metric" | "change";

export interface EvidenceItem {
  id: string;
  evidence_id: string;
  kind: EvidenceKind;
  source: string;
  summary: string;
  observed_at: string;
  attributes: Record<string, JsonValue>;
  content_hash: string;
  created_at: string;
}

export interface EvidenceBoardSnapshot {
  investigation_run_id: string;
  status: InvestigationRunStatus;
  evidence_items: EvidenceItem[];
}
