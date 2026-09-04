import { afterEach, describe, expect, it, vi } from "vitest";

import {
  closeIncident,
  createIncidentReport,
  decideApprovalRequest,
  createVerificationRun,
  executeActionStep,
  getEvaluationRun,
  getEvidenceBoard,
  getIncidentWorkroom,
  getIncidentReport,
  getVerificationRun,
  getVerificationWorkItem,
  listEvaluationRuns,
  listExecutionCandidates,
  listIncidents,
  listInvestigationAttempts,
  listPendingApprovals,
  listVerificationCandidates,
} from "./api";

describe("控制面 API 客户端", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("关闭事故时只发送审计字段和 Incident 乐观锁", async () => {
    const body = { id: "incident-001", status: "closed", version: 7 };
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(body), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const input = {
      expected_version: 6,
      actor_id: "engineer-001",
      reason: "恢复验证通过，正式关闭事故。",
    };

    await expect(closeIncident("incident/001", input)).resolves.toEqual(body);
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/incidents/incident%2F001/actions/close",
      expect.objectContaining({
        method: "POST",
        headers: { Accept: "application/json", "Content-Type": "application/json" },
        body: JSON.stringify(input),
      }),
    );
  });

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

  it("使用编码后的 Incident ID 生成并读取确定性复盘报告", async () => {
    const report = { id: "report-001", incident_id: "incident/001" };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ created: true, report }), {
          status: 201,
          headers: { "Content-Type": "application/json" },
        }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify(report), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      );
    vi.stubGlobal("fetch", fetchMock);
    const input = { expected_incident_version: 7, actor_id: "engineer-001" };

    await expect(createIncidentReport("incident/001", input)).resolves.toEqual({
      created: true,
      report,
    });
    await expect(getIncidentReport("incident/001")).resolves.toEqual(report);
    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      "/api/v1/incidents/incident%2F001/reports",
      expect.objectContaining({ method: "POST", body: JSON.stringify(input) }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "/api/v1/incidents/incident%2F001/reports/current",
      expect.objectContaining({ method: "GET" }),
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

  it("读取有界待验证操作队列", async () => {
    const body = { items: [], total: 0 };
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(body), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(listVerificationCandidates()).resolves.toEqual(body);
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/verification-candidates?limit=50",
      expect.objectContaining({ method: "GET", headers: { Accept: "application/json" } }),
    );
  });

  it("创建验证时只发送 Incident 乐观锁并编码执行 ID", async () => {
    const body = { created: true, verification_run: { id: "verification-001" } };
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(body), {
        status: 201,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const input = { expected_incident_version: 5 };

    await expect(createVerificationRun("execution/001", input)).resolves.toEqual(body);
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/operation-executions/execution%2F001/verification-runs",
      expect.objectContaining({
        method: "POST",
        headers: { Accept: "application/json", "Content-Type": "application/json" },
        body: JSON.stringify(input),
      }),
    );
  });

  it("只读查询验证运行和 Worker 调度状态", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ id: "run/001" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ verification_run_id: "run/001" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      );
    vi.stubGlobal("fetch", fetchMock);

    await getVerificationRun("run/001");
    await getVerificationWorkItem("run/001");

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      "/api/v1/verification-runs/run%2F001",
      expect.objectContaining({ method: "GET" }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "/api/v1/verification-runs/run%2F001/work-item",
      expect.objectContaining({ method: "GET" }),
    );
  });
});
