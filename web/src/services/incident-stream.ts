import { apiUrl } from "./api";
import { operatorAuthorization } from "./operator-session";
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

function connectAuthorizedEvents(
  url: string,
  authorization: string,
  handlers: IncidentStreamHandlers,
): IncidentStreamConnection {
  const controller = new AbortController();
  const { signal } = controller;
  let cursor = 0;
  let reconnect: ReturnType<typeof setTimeout> | undefined;

  async function consume(): Promise<void> {
    try {
      const response = await fetch(url, {
        headers: {
          Accept: "text/event-stream",
          Authorization: authorization,
          ...(cursor ? { "Last-Event-ID": String(cursor) } : {}),
        },
        cache: "no-store",
        redirect: "error",
        signal,
      });
      if (signal.aborted) return;
      if (response.status === 401 || response.status === 403) {
        handlers.onMalformedEvent("事件流认证已失效，请重新验证身份。");
        handlers.onStatus("closed");
        return;
      }
      if (!response.ok || !response.body) throw new Error("无法读取事件流");
      handlers.onStatus("open");
      const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
      let pending = "";
      try {
        while (!signal.aborted) {
          const { done, value } = await reader.read();
          if (done || signal.aborted) break;
          pending += value;
          const frames = pending.split(/\r?\n\r?\n/);
          pending = frames.pop() ?? "";
          for (const frame of frames) {
            const data = frame.split(/\r?\n/)
              .filter((line) => line.startsWith("data:"))
              .map((line) => line.slice(5).replace(/^ /, ""));
            if (data.length === 0) continue;
            try {
              const event = parseIncidentEvent(data.join("\n"));
              if (Number.isSafeInteger(event.cursor) && event.cursor > cursor) {
                handlers.onEvent(event);
                cursor = event.cursor;
              }
            } catch (error) {
              handlers.onMalformedEvent(error instanceof Error ? error.message : "事件格式无效");
            }
          }
        }
      } finally {
        reader.releaseLock();
      }
    } catch {
      // 连接中断后仅从已交付游标恢复。
    }
    if (!signal.aborted) {
      handlers.onStatus("reconnecting");
      reconnect = setTimeout(() => void consume(), 1000);
    }
  }
  void consume();
  return {
    close: () => {
      controller.abort();
      clearTimeout(reconnect);
      handlers.onStatus("closed");
    },
  };
}

export function connectIncidentEvents(
  incidentId: string,
  handlers: IncidentStreamHandlers,
  createEventSource: EventSourceFactory = (url) => new EventSource(url),
): IncidentStreamConnection {
  handlers.onStatus("connecting");
  const url = apiUrl(`/api/v1/incidents/${encodeURIComponent(incidentId)}/stream`);
  const authorization = operatorAuthorization();
  if (authorization) return connectAuthorizedEvents(url, authorization, handlers);
  const source = createEventSource(url);

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
