import { createPinia, setActivePinia } from "pinia";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { IncidentStreamHandlers } from "../services/incident-stream";
import type { Incident, IncidentEvent } from "../types/incidents";
import { useIncidentStore } from "./incident-store";

const mocks = vi.hoisted(() => ({
  listIncidents: vi.fn(),
  getIncident: vi.fn(),
  connectIncidentEvents: vi.fn(),
}));

vi.mock("../services/api", () => ({
  listIncidents: mocks.listIncidents,
  getIncident: mocks.getIncident,
}));

vi.mock("../services/incident-stream", () => ({
  connectIncidentEvents: mocks.connectIncidentEvents,
}));

const INCIDENT: Incident = {
  id: "incident-001",
  display_number: 1,
  reference: "INC-000001",
  service_name: "order-service",
  title: "订单接口大量返回 500",
  severity: "p1",
  status: "verifying",
  fingerprint: "order-service:http-500",
  version: 3,
  started_at: "2026-01-01T07:59:00Z",
  resolved_at: null,
  closed_at: null,
  created_at: "2026-01-01T08:00:00Z",
  updated_at: "2026-01-01T08:02:00Z",
};

function event(cursor: number, version: number): IncidentEvent {
  return {
    cursor,
    incident_id: INCIDENT.id,
    event_type: "incident.status_changed",
    actor_type: "system",
    actor_id: null,
    from_status: "verifying",
    to_status: "resolved",
    reason: "恢复验证通过",
    incident_version: version,
    payload: { action: "confirm_recovery" },
    occurred_at: "2026-01-01T08:03:00Z",
  };
}

describe("Incident 状态仓库", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    mocks.listIncidents.mockReset();
    mocks.getIncident.mockReset();
    mocks.connectIncidentEvents.mockReset();
  });

  it("按游标去重，并阻止历史事件或迟到列表覆盖更新版本", async () => {
    const handlers: { current: IncidentStreamHandlers | null } = { current: null };
    mocks.listIncidents.mockResolvedValue({ items: [INCIDENT], next_before_display_number: null });
    mocks.getIncident.mockResolvedValue(INCIDENT);
    mocks.connectIncidentEvents.mockImplementation(
      (_incidentId: string, receivedHandlers: IncidentStreamHandlers) => {
        handlers.current = receivedHandlers;
        return { close: vi.fn() };
      },
    );
    const store = useIncidentStore();

    await store.loadIncidents();
    await store.selectIncident(INCIDENT.id);
    if (handlers.current === null) {
      throw new Error("SSE 处理器未注册");
    }
    handlers.current.onEvent(event(1, 1));
    handlers.current.onEvent(event(8, 4));
    handlers.current.onEvent(event(8, 4));
    await store.loadIncidents();

    expect(store.events.map((item) => item.cursor)).toEqual([1, 8]);
    expect(store.selectedIncident?.status).toBe("resolved");
    expect(store.selectedIncident?.version).toBe(4);
    expect(store.incidents[0]?.version).toBe(4);
  });
});
