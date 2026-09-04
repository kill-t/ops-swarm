import { createPinia, setActivePinia } from "pinia";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  CREATED_VERIFICATION_RESPONSE,
  READY_VERIFICATION_CANDIDATE,
  RUNNING_VERIFICATION_CANDIDATE,
  VERIFICATION_RUN,
  VERIFICATION_WORK_ITEM,
} from "../test-support/verification-fixtures";
import { useVerificationStore } from "./verification-store";

const mocks = vi.hoisted(() => ({
  listVerificationCandidates: vi.fn(),
  createVerificationRun: vi.fn(),
  getVerificationRun: vi.fn(),
  getVerificationWorkItem: vi.fn(),
}));

vi.mock("../services/api", () => mocks);

describe("恢复验证状态仓库", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    Object.values(mocks).forEach((mock) => mock.mockReset());
  });

  it("用成功执行 ID 和精确 Incident 版本创建验证", async () => {
    mocks.listVerificationCandidates
      .mockResolvedValueOnce({ items: [READY_VERIFICATION_CANDIDATE], total: 1 })
      .mockResolvedValueOnce({ items: [RUNNING_VERIFICATION_CANDIDATE], total: 1 });
    mocks.createVerificationRun.mockResolvedValue(CREATED_VERIFICATION_RESPONSE);
    mocks.getVerificationWorkItem.mockResolvedValue(VERIFICATION_WORK_ITEM);
    const store = useVerificationStore();
    await store.loadCandidates();
    store.selectCandidate(READY_VERIFICATION_CANDIDATE.operation_execution.id);

    await expect(store.createRun()).resolves.toBe(true);

    expect(mocks.createVerificationRun).toHaveBeenCalledWith("execution-001", {
      expected_incident_version: 5,
    });
    expect(store.observedRun).toEqual(VERIFICATION_RUN);
    expect(store.observedWorkItem).toEqual(VERIFICATION_WORK_ITEM);
    expect(store.actionMessage).toContain("自动采样");
  });

  it("刷新到终态后移除队列项但保留最后结果", async () => {
    const passedRun = {
      ...VERIFICATION_RUN,
      status: "passed" as const,
      successful_sample_count: 3,
      last_observed_at: "2099-09-01T09:08:00Z",
      updated_at: "2099-09-01T09:08:00Z",
      completed_at: "2099-09-01T09:08:00Z",
    };
    const completedWork = {
      ...VERIFICATION_WORK_ITEM,
      status: "completed" as const,
      completed_at: "2099-09-01T09:08:00Z",
    };
    mocks.listVerificationCandidates
      .mockResolvedValueOnce({ items: [RUNNING_VERIFICATION_CANDIDATE], total: 1 })
      .mockResolvedValueOnce({ items: [], total: 0 });
    mocks.getVerificationRun.mockResolvedValue(passedRun);
    mocks.getVerificationWorkItem.mockResolvedValue(completedWork);
    const store = useVerificationStore();
    await store.loadCandidates();
    store.selectCandidate(RUNNING_VERIFICATION_CANDIDATE.operation_execution.id);

    await expect(store.refreshProgress()).resolves.toBe(true);

    expect(store.items).toEqual([]);
    expect(store.selectedExecutionId).toBeNull();
    expect(store.observedRun?.status).toBe("passed");
    expect(store.observedCandidate).toEqual(RUNNING_VERIFICATION_CANDIDATE);
    expect(store.actionMessage).toContain("resolved");
  });

  it("已有运行时不会再次调用创建接口", async () => {
    mocks.listVerificationCandidates.mockResolvedValue({
      items: [RUNNING_VERIFICATION_CANDIDATE],
      total: 1,
    });
    const store = useVerificationStore();
    await store.loadCandidates();
    store.selectCandidate(RUNNING_VERIFICATION_CANDIDATE.operation_execution.id);

    await expect(store.createRun()).resolves.toBe(true);

    expect(mocks.createVerificationRun).not.toHaveBeenCalled();
    expect(store.actionMessage).toContain("已有恢复验证");
  });
});
