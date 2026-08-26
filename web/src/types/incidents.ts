export const incidentStatuses = [
  "open",
  "investigating",
  "awaiting_approval",
  "mitigating",
  "verifying",
  "resolved",
  "closed",
] as const;

export type IncidentStatus = (typeof incidentStatuses)[number];
export type IncidentSeverity = "p1" | "p2" | "p3" | "p4";
export type ActorType = "system" | "human" | "agent" | "executor";

export const incidentEventTypes = [
  "incident.created",
  "incident.status_changed",
  "incident.alert_attached",
] as const;

export type IncidentEventType = (typeof incidentEventTypes)[number];
export type JsonValue = string | number | boolean | null | JsonValue[] | JsonObject;
export type JsonObject = { [key: string]: JsonValue };

export interface Incident {
  id: string;
  display_number: number;
  reference: string;
  service_name: string;
  title: string;
  severity: IncidentSeverity;
  status: IncidentStatus;
  fingerprint: string;
  version: number;
  started_at: string;
  resolved_at: string | null;
  closed_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface IncidentPage {
  items: Incident[];
  next_before_display_number: number | null;
}

export interface IncidentEvent {
  cursor: number;
  incident_id: string;
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

export type StreamStatus = "idle" | "connecting" | "open" | "reconnecting" | "closed";
