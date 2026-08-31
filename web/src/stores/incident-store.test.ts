import { createPinia, setActivePinia } from "pinia";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { IncidentStreamHandlers } from "../services/incident-stream";
import type { EvidenceBoardSnapshot } from "../types/evidence-board";
import type { Incident, IncidentEvent } from "../types/incidents";
import type {
  InvestigationAttempt,
  InvestigationAttemptList,
} from "../types/investigation-attempts";
import type { IncidentWorkroomSnapshot } from "../types/workroom";
import { useIncidentStore } from "./incident-store";

const mocks = vi.hoisted(() => ({
  listIncidents: vi.fn(),
  getIncident: vi.fn(),
  getIncidentWorkroom: vi.fn(),
  getEvidenceBoard: vi.fn(),
  listInvestigationAttempts: vi.fn(),
  connectIncidentEvents: vi.fn(),
}));

vi.mock("../services/api", () => ({
  listIncidents: mocks.listIncidents,
  getIncident: mocks.getIncident,
  getIncidentWorkroom: mocks.getIncidentWorkroom,
  getEvidenceBoard: mocks.getEvidenceBoard,
  listInvestigationAttempts: mocks.listInvestigationAttempts,
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

const WORKROOM: IncidentWorkroomSnapshot = {
  incident_id: INCIDENT.id,
  incident_version: INCIDENT.version,
  investigation: null,
  evidence_board: null,
  action_plan: null,
  approval_request: null,
  operation_execution: null,
  verification_run: null,
};

const WORKROOM_WITH_INVESTIGATION: IncidentWorkroomSnapshot = {
  ...WORKROOM,
  investigation: {
    id: "run-001",
    mode: "single_agent",
    data_source_mode: "live",
    status: "running",
    prompt_version: "single-agent-v1",
    model_name: "deepseek-v4-flash",
    error_code: null,
    error_message: null,
    created_at: "2026-01-01T08:00:00Z",
    started_at: "2026-01-01T08:00:01Z",
    completed_at: null,
    updated_at: "2026-01-01T08:00:01Z",
  },
};

const ATTEMPT: InvestigationAttempt = {
  id: "attempt-001",
  work_item_id: "work-001",
  attempt_number: 1,
  status: "running",
  worker_id: "live-worker",
  model_name: "deepseek-v4-flash",
  pricing: null,
  usage: {
    model_request_count: 0,
    usage_complete: false,
    input_tokens: 0,
    output_tokens: 0,
    total_tokens: 0,
    estimated_cost_usd: null,
  },
  started_at: "2026-01-01T08:00:01Z",
  lease_expires_at: "2026-01-01T08:02:01Z",
  finished_at: null,
  duration_ms: null,
  will_retry: null,
  error: null,
};

function evidenceBoard(runId: string, evidenceId = "deployment-evidence"): EvidenceBoardSnapshot {
  return {
    investigation_run_id: runId,
    status: "succeeded",
    evidence_items: [
      {
        id: `stored-${evidenceId}`,
        evidence_id: evidenceId,
        kind: "change",
        source: "change-simulator",
        summary: "已保存部署代码来源。",
        observed_at: "2026-01-01T08:00:01Z",
        attributes: { change_type: "deployment" },
        content_hash: "a".repeat(64),
        created_at: "2026-01-01T08:00:02Z",
      },
    ],
  };
}

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
    mocks.getIncidentWorkroom.mockReset();
    mocks.getEvidenceBoard.mockReset();
    mocks.listInvestigationAttempts.mockReset();
    mocks.connectIncidentEvents.mockReset();
    mocks.getIncidentWorkroom.mockResolvedValue(WORKROOM);
    mocks.listInvestigationAttempts.mockResolvedValue({
      investigation_run_id: "run-001",
      items: [],
    });
    mocks.getEvidenceBoard.mockImplementation((runId: string) =>
      Promise.resolve(evidenceBoard(runId)),
    );
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
    expect(store.workroom?.incident_id).toBe(INCIDENT.id);
    await vi.waitFor(() => expect(mocks.getIncidentWorkroom).toHaveBeenCalledTimes(2));
  });

  it("只为当前调查运行读取尝试并随手工同步刷新", async () => {
    mocks.getIncident.mockResolvedValue(INCIDENT);
    mocks.getIncidentWorkroom.mockResolvedValue(WORKROOM_WITH_INVESTIGATION);
    mocks.listInvestigationAttempts.mockResolvedValue({
      investigation_run_id: "run-001",
      items: [ATTEMPT],
    });
    mocks.connectIncidentEvents.mockReturnValue({ close: vi.fn() });
    const store = useIncidentStore();

    await store.selectIncident(INCIDENT.id);

    expect(mocks.listInvestigationAttempts).toHaveBeenCalledWith("run-001");
    expect(mocks.getEvidenceBoard).toHaveBeenCalledWith("run-001");
    expect(store.investigationAttempts).toEqual([ATTEMPT]);
    expect(store.evidenceBoard).toEqual(evidenceBoard("run-001"));
    expect(store.attemptsError).toBeNull();
    expect(store.evidenceBoardError).toBeNull();

    await store.refreshSelectedWorkroom();

    expect(mocks.listInvestigationAttempts).toHaveBeenCalledTimes(2);
    expect(mocks.getEvidenceBoard).toHaveBeenCalledTimes(2);
  });

  it("调查运行切换后忽略较早返回的尝试响应", async () => {
    const secondWorkroom: IncidentWorkroomSnapshot = {
      ...WORKROOM_WITH_INVESTIGATION,
      investigation: {
        ...WORKROOM_WITH_INVESTIGATION.investigation!,
        id: "run-002",
      },
    };
    const secondAttempt: InvestigationAttempt = {
      ...ATTEMPT,
      id: "attempt-002",
      work_item_id: "work-002",
    };
    let resolveFirst: (response: InvestigationAttemptList) => void = () => undefined;
    const firstResponse = new Promise<InvestigationAttemptList>((resolve) => {
      resolveFirst = resolve;
    });
    mocks.getIncident.mockResolvedValue(INCIDENT);
    mocks.getIncidentWorkroom
      .mockResolvedValueOnce(WORKROOM_WITH_INVESTIGATION)
      .mockResolvedValueOnce(secondWorkroom);
    mocks.listInvestigationAttempts.mockImplementation((runId: string) =>
      runId === "run-001"
        ? firstResponse
        : Promise.resolve({ investigation_run_id: "run-002", items: [secondAttempt] }),
    );
    mocks.connectIncidentEvents.mockReturnValue({ close: vi.fn() });
    const store = useIncidentStore();

    const initialSelection = store.selectIncident(INCIDENT.id);
    await vi.waitFor(() => expect(mocks.listInvestigationAttempts).toHaveBeenCalledWith("run-001"));
    await store.refreshSelectedWorkroom();
    resolveFirst({ investigation_run_id: "run-001", items: [ATTEMPT] });
    await initialSelection;

    expect(store.workroom?.investigation?.id).toBe("run-002");
    expect(store.investigationAttempts).toEqual([secondAttempt]);
  });

  it("调查运行切换后忽略较早返回的 Evidence Board", async () => {
    const secondWorkroom: IncidentWorkroomSnapshot = {
      ...WORKROOM_WITH_INVESTIGATION,
      investigation: {
        ...WORKROOM_WITH_INVESTIGATION.investigation!,
        id: "run-002",
      },
    };
    let resolveFirst: (response: EvidenceBoardSnapshot) => void = () => undefined;
    const firstResponse = new Promise<EvidenceBoardSnapshot>((resolve) => {
      resolveFirst = resolve;
    });
    mocks.getIncident.mockResolvedValue(INCIDENT);
    mocks.getIncidentWorkroom
      .mockResolvedValueOnce(WORKROOM_WITH_INVESTIGATION)
      .mockResolvedValueOnce(secondWorkroom);
    mocks.getEvidenceBoard.mockImplementation((runId: string) =>
      runId === "run-001"
        ? firstResponse
        : Promise.resolve(evidenceBoard("run-002", "deployment-evidence-002")),
    );
    mocks.connectIncidentEvents.mockReturnValue({ close: vi.fn() });
    const store = useIncidentStore();

    const initialSelection = store.selectIncident(INCIDENT.id);
    await vi.waitFor(() => expect(mocks.getEvidenceBoard).toHaveBeenCalledWith("run-001"));
    await store.refreshSelectedWorkroom();
    resolveFirst(evidenceBoard("run-001", "stale-deployment-evidence"));
    await initialSelection;

    expect(store.workroom?.investigation?.id).toBe("run-002");
    expect(store.evidenceBoard).toEqual(
      evidenceBoard("run-002", "deployment-evidence-002"),
    );
  });
});
