import type {
  ActorType,
  IncidentEventType,
  IncidentSeverity,
  IncidentStatus,
  JsonObject,
} from "./incidents";
import type { IncidentWorkroomSnapshot } from "./workroom";

export interface IncidentReportIncident {
  id: string;
  display_number: number;
  reference: string;
  service_name: string;
  title: string;
  severity: IncidentSeverity;
  status: "closed";
  fingerprint: string;
  version: number;
  started_at: string;
  resolved_at: string;
  closed_at: string;
  duration_seconds: number;
}

export interface IncidentReportTimelineEntry {
  cursor: number;
  event_type: IncidentEventType;
  actor_type: ActorType;
  actor_id: string | null;
  from_status: IncidentStatus | null;
  to_status: IncidentStatus;
  reason: string;
  incident_version: number;
  payload: JsonObject;
  occurred_at: string;
}

export interface IncidentReportContent {
  schema_version: "incident-report.v1";
  generated_at: string;
  generated_by_id: string;
  incident: IncidentReportIncident;
  workroom: IncidentWorkroomSnapshot;
  timeline: IncidentReportTimelineEntry[];
}

export interface IncidentReport {
  id: string;
  incident_id: string;
  incident_version: number;
  schema_version: "incident-report.v1";
  content_hash: string;
  content: IncidentReportContent;
  created_at: string;
}

export interface CreateIncidentReportInput {
  expected_incident_version: number;
  actor_id: string;
}

export interface CreateIncidentReportResponse {
  created: boolean;
  report: IncidentReport;
}
