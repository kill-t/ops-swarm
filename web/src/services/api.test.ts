import { afterEach, describe, expect, it, vi } from "vitest";

import {
  decideApprovalRequest,
  executeActionStep,
  getEvaluationRun,
  getEvidenceBoard,
  getIncidentWorkroom,
  listEvaluationRuns,
  listExecutionCandidates,
  listIncidents,
  listInvestigationAttempts,
  listPendingApprovals,
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

  it("使用编码后的调查运行 ID 读取 Evidence Board", async () => {
    const body = {
      investigation_run_id: "run/001",
      status: "succeeded",
      evidence_items: [],
    };
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(body), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(getEvidenceBoard("run/001")).resolves.toEqual(body);
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/investigations/run%2F001/evidence-board",
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

  it("读取有界待审批队列", async () => {
    const body = { items: [], total: 0 };
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(body), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(listPendingApprovals()).resolves.toEqual(body);
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/approval-requests?limit=50",
      expect.objectContaining({ method: "GET", headers: { Accept: "application/json" } }),
    );
  });

  it("批准时只发送人工审计与 Incident 乐观锁", async () => {
    const body = { decision_applied: true, approval_request: { status: "approved" } };
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(body), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const input = {
      expected_incident_version: 3,
      actor_id: "engineer-001",
      reason: "确认风险和恢复判据",
    };

    await expect(decideApprovalRequest("approval/001", "approve", input)).resolves.toEqual(body);
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/approval-requests/approval%2F001/actions/approve",
      expect.objectContaining({
        method: "POST",
        headers: { Accept: "application/json", "Content-Type": "application/json" },
        body: JSON.stringify(input),
      }),
    );
  });

  it("读取有界待执行计划队列", async () => {
    const body = { items: [], total: 0 };
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(body), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(listExecutionCandidates()).resolves.toEqual(body);
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/execution-candidates?limit=50",
      expect.objectContaining({ method: "GET", headers: { Accept: "application/json" } }),
    );
  });

  it("执行时只发送幂等键和 Incident 乐观锁", async () => {
    const body = { created: true, execution_performed: true, operation_execution: {} };
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(body), {
        status: 201,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const input = {
      idempotency_key: "ui-exec:approval-001:step-001",
      expected_incident_version: 4,
    };

    await expect(executeActionStep("step/001", input)).resolves.toEqual(body);
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/action-steps/step%2F001/executions",
      expect.objectContaining({
        method: "POST",
        headers: { Accept: "application/json", "Content-Type": "application/json" },
        body: JSON.stringify(input),
      }),
    );
  });
});
