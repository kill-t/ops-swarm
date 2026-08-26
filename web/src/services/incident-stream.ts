import { apiUrl } from "./api";
import {
  incidentEventTypes,
  incidentStatuses,
  type IncidentEvent,
  type IncidentStatus,
  type StreamStatus,
} from "../types/incidents";

export interface IncidentStreamHandlers {
  onEvent: (event: IncidentEvent) => void;
  onStatus: (status: StreamStatus) => void;
  onMalformedEvent: (message: string) => void;
}

export interface IncidentStreamConnection {
  close: () => void;
}

export type EventSourceFactory = (url: string) => EventSource;

const actorTypes = ["system", "human", "agent", "executor"] as const;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function parseIncidentEvent(rawData: string): IncidentEvent {
  const value: unknown = JSON.parse(rawData);
  if (
    !isRecord(value) ||
    typeof value.cursor !== "number" ||
    typeof value.incident_id !== "string" ||
    typeof value.event_type !== "string" ||
    !incidentEventTypes.includes(value.event_type as IncidentEvent["event_type"]) ||
    typeof value.actor_type !== "string" ||
    !actorTypes.includes(value.actor_type as IncidentEvent["actor_type"]) ||
    (value.actor_id !== null && typeof value.actor_id !== "string") ||
    (value.from_status !== null &&
      (typeof value.from_status !== "string" ||
        !incidentStatuses.includes(value.from_status as IncidentStatus))) ||
    typeof value.to_status !== "string" ||
    !incidentStatuses.includes(value.to_status as IncidentEvent["to_status"]) ||
    typeof value.reason !== "string" ||
    typeof value.incident_version !== "number" ||
    !isRecord(value.payload) ||
    typeof value.occurred_at !== "string"
  ) {
    throw new Error("SSE 事件缺少必要字段");
  }
  return value as unknown as IncidentEvent;
}

export function connectIncidentEvents(
  incidentId: string,
  handlers: IncidentStreamHandlers,
  createEventSource: EventSourceFactory = (url) => new EventSource(url),
): IncidentStreamConnection {
  handlers.onStatus("connecting");
  const source = createEventSource(
    apiUrl(`/api/v1/incidents/${encodeURIComponent(incidentId)}/stream`),
  );

  const receiveEvent = (rawEvent: Event): void => {
    try {
      const message = rawEvent as MessageEvent<string>;
      handlers.onEvent(parseIncidentEvent(message.data));
    } catch (error) {
      const message = error instanceof Error ? error.message : "无法解析 SSE 事件";
      handlers.onMalformedEvent(message);
    }
  };

  for (const eventType of incidentEventTypes) {
    source.addEventListener(eventType, receiveEvent);
  }
  source.onopen = () => handlers.onStatus("open");
  source.onerror = () => handlers.onStatus("reconnecting");

  return {
    close: () => {
      source.close();
      handlers.onStatus("closed");
    },
  };
}
