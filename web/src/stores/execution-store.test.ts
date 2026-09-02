import { createPinia, setActivePinia } from "pinia";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  EXECUTION_CANDIDATE,
  SUCCEEDED_EXECUTION_RESPONSE,
} from "../test-support/execution-fixtures";
import { executionIdempotencyKey, useExecutionStore } from "./execution-store";

const mocks = vi.hoisted(() => ({
  listExecutionCandidates: vi.fn(),
  executeActionStep: vi.fn(),
}));

vi.mock("../services/api", () => ({
  listExecutionCandidates: mocks.listExecutionCandidates,
  executeActionStep: mocks.executeActionStep,
}));

describe("受控执行状态仓库", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    mocks.listExecutionCandidates.mockReset();
    mocks.executeActionStep.mockReset();
  });

  it("用稳定幂等键和精确 Incident 版本执行白名单步骤", async () => {
    mocks.listExecutionCandidates
      .mockResolvedValueOnce({ items: [EXECUTION_CANDIDATE], total: 1 })
      .mockResolvedValueOnce({ items: [], total: 0 });
    mocks.executeActionStep.mockResolvedValue(SUCCEEDED_EXECUTION_RESPONSE);
    const store = useExecutionStore();
    await store.loadCandidates();
    store.selectCandidate(EXECUTION_CANDIDATE.action_plan.id);

    const executed = await store.execute("step-001");

    expect(executed).toBe(true);
    expect(mocks.executeActionStep).toHaveBeenCalledWith("step-001", {
      idempotency_key: "ui-exec:approval-001:step-001",
      expected_incident_version: 4,
    });
    expect(store.items).toEqual([]);
    expect(store.lastOutcome).toEqual(SUCCEEDED_EXECUTION_RESPONSE);
    expect(store.actionMessage).toContain("恢复验证阶段");
  });

  it("执行冲突时显示错误并刷新服务端候选", async () => {
    mocks.listExecutionCandidates
      .mockResolvedValueOnce({ items: [EXECUTION_CANDIDATE], total: 1 })
      .mockResolvedValueOnce({ items: [], total: 0 });
    mocks.executeActionStep.mockRejectedValue(new Error("Incident 版本已经变化"));
    const store = useExecutionStore();
    await store.loadCandidates();
    store.selectCandidate(EXECUTION_CANDIDATE.action_plan.id);

    const executed = await store.execute("step-001");

    expect(executed).toBe(false);
    expect(store.actionError).toBe("Incident 版本已经变化");
    expect(mocks.listExecutionCandidates).toHaveBeenCalledTimes(2);
    expect(store.selectedCandidate).toBeNull();
  });

  it("已有运行记录时不创建不同幂等操作", async () => {
    const runningCandidate = {
      ...EXECUTION_CANDIDATE,
      active_executions: [
        {
          ...SUCCEEDED_EXECUTION_RESPONSE.operation_execution,
          status: "running" as const,
          idempotency_key: "cli-exec-running-001",
          locked_until: "2099-09-01T09:07:00Z",
          result: null,
          completed_at: null,
        },
      ],
    };
    mocks.listExecutionCandidates.mockResolvedValue({ items: [runningCandidate], total: 1 });
    const store = useExecutionStore();
    await store.loadCandidates();
    store.selectCandidate(runningCandidate.action_plan.id);

    await expect(store.execute("step-001")).resolves.toBe(false);
    expect(mocks.executeActionStep).not.toHaveBeenCalled();
    expect(store.actionError).toContain("已有运行中的操作");
  });

  it("幂等键由审批请求和步骤稳定派生", () => {
    expect(executionIdempotencyKey("approval-001", "step-001")).toBe(
      "ui-exec:approval-001:step-001",
    );
  });
});
