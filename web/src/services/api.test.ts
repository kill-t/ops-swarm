import { afterEach, describe, expect, it, vi } from "vitest";

import {
  getEvaluationRun,
  getIncidentWorkroom,
  listEvaluationRuns,
  listIncidents,
  listInvestigationAttempts,
} from "./api";

describe("控制面 API 客户端", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("读取事故列表并保持服务端游标结构", async () => {
    const body = { items: [], next_before_display_number: null };
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(body), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(listIncidents()).resolves.toEqual(body);
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/incidents?limit=100",
      expect.objectContaining({ headers: { Accept: "application/json" } }),
    );
  });

  it("把结构化后端错误转换为稳定客户端错误", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({ error_code: "incident_not_found", message: "Incident 不存在" }),
          { status: 404, headers: { "Content-Type": "application/json" } },
        ),
      ),
    );

    const request = listIncidents();
    await expect(request).rejects.toMatchObject({
      status: 404,
      errorCode: "incident_not_found",
      message: "Incident 不存在",
    });
  });

  it("使用编码后的 Incident ID 读取作战室聚合快照", async () => {
    const body = {
      incident_id: "incident/001",
      incident_version: 1,
      investigation: null,
      evidence_board: null,
      action_plan: null,
      approval_request: null,
      operation_execution: null,
      verification_run: null,
    };
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(body), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(getIncidentWorkroom("incident/001")).resolves.toEqual(body);
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/incidents/incident%2F001/workroom",
      expect.objectContaining({ headers: { Accept: "application/json" } }),
    );
  });

  it("使用编码后的调查运行 ID 读取尝试历史", async () => {
    const body = { investigation_run_id: "run/001", items: [] };
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(body), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(listInvestigationAttempts("run/001")).resolves.toEqual(body);
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/investigations/run%2F001/attempts",
      expect.objectContaining({ headers: { Accept: "application/json" } }),
    );
  });

  it("使用服务端游标读取评测历史", async () => {
    const body = { items: [], next_before_sequence: null };
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(body), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(listEvaluationRuns(42)).resolves.toEqual(body);
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/evaluation-runs?limit=50&before_sequence=42",
      expect.objectContaining({ headers: { Accept: "application/json" } }),
    );
  });

  it("编码评测运行 ID 后读取完整报告", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ id: "run/001" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await getEvaluationRun("run/001");
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/evaluation-runs/run%2F001",
      expect.objectContaining({ headers: { Accept: "application/json" } }),
    );
  });
});
