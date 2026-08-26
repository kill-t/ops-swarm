import { describe, expect, it, vi } from "vitest";

import { connectIncidentEvents } from "./incident-stream";
import type { IncidentEvent, StreamStatus } from "../types/incidents";

class FakeEventSource {
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  readonly listeners = new Map<string, EventListener>();
  closed = false;

  addEventListener(type: string, listener: EventListener): void {
    this.listeners.set(type, listener);
  }

  emit(type: string, data: string): void {
    this.listeners.get(type)?.({ data } as unknown as Event);
  }

  close(): void {
    this.closed = true;
  }
}

describe("Incident SSE 客户端", () => {
  it("监听具名事件并在关闭时停止原生连接", () => {
    const source = new FakeEventSource();
    const events: IncidentEvent[] = [];
    const statuses: StreamStatus[] = [];
    const malformed = vi.fn();
    const connection = connectIncidentEvents(
      "incident-001",
      {
        onEvent: (event) => events.push(event),
        onStatus: (status) => statuses.push(status),
        onMalformedEvent: malformed,
      },
      (url) => {
        expect(url).toBe("/api/v1/incidents/incident-001/stream");
        return source as unknown as EventSource;
      },
    );

    source.onopen?.();
    source.emit(
      "incident.status_changed",
      JSON.stringify({
        cursor: 12,
        incident_id: "incident-001",
        event_type: "incident.status_changed",
        actor_type: "human",
        actor_id: "engineer-001",
        from_status: "resolved",
        to_status: "closed",
        reason: "确认关闭",
        incident_version: 5,
        payload: { action: "close" },
        occurred_at: "2026-01-01T08:00:00Z",
      }),
    );
    connection.close();

    expect(events).toHaveLength(1);
    expect(events[0]?.cursor).toBe(12);
    expect(statuses).toEqual(["connecting", "open", "closed"]);
    expect(source.closed).toBe(true);
    expect(malformed).not.toHaveBeenCalled();
  });

  it("拒绝缺少游标的畸形事件但保持连接", () => {
    const source = new FakeEventSource();
    const malformed = vi.fn();
    connectIncidentEvents(
      "incident-001",
      {
        onEvent: vi.fn(),
        onStatus: vi.fn(),
        onMalformedEvent: malformed,
      },
      () => source as unknown as EventSource,
    );

    source.emit("incident.created", JSON.stringify({ incident_id: "incident-001" }));

    expect(malformed).toHaveBeenCalledWith("SSE 事件缺少必要字段");
    expect(source.closed).toBe(false);
  });

  it("拒绝未知状态，避免畸形事件污染前端状态", () => {
    const source = new FakeEventSource();
    const malformed = vi.fn();
    const onEvent = vi.fn();
    connectIncidentEvents(
      "incident-001",
      {
        onEvent,
        onStatus: vi.fn(),
        onMalformedEvent: malformed,
      },
      () => source as unknown as EventSource,
    );

    source.emit(
      "incident.status_changed",
      JSON.stringify({
        cursor: 13,
        incident_id: "incident-001",
        event_type: "incident.status_changed",
        actor_type: "system",
        actor_id: null,
        from_status: "open",
        to_status: "unknown",
        reason: "非法状态",
        incident_version: 2,
        payload: {},
        occurred_at: "2026-01-01T08:00:00Z",
      }),
    );

    expect(onEvent).not.toHaveBeenCalled();
    expect(malformed).toHaveBeenCalledWith("SSE 事件缺少必要字段");
    expect(source.closed).toBe(false);
  });
});
