import { createPinia, setActivePinia } from "pinia";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { PENDING_APPROVAL } from "../test-support/approval-fixtures";
import { useApprovalStore } from "./approval-store";

const mocks = vi.hoisted(() => ({
  listPendingApprovals: vi.fn(),
  decideApprovalRequest: vi.fn(),
}));

vi.mock("../services/api", () => ({
  listPendingApprovals: mocks.listPendingApprovals,
  decideApprovalRequest: mocks.decideApprovalRequest,
}));

describe("审批中心状态仓库", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    mocks.listPendingApprovals.mockReset();
    mocks.decideApprovalRequest.mockReset();
  });

  it("读取待办并提交精确 Incident 版本的批准决定", async () => {
    mocks.listPendingApprovals
      .mockResolvedValueOnce({ items: [PENDING_APPROVAL], total: 1 })
      .mockResolvedValueOnce({ items: [], total: 0 });
    mocks.decideApprovalRequest.mockResolvedValue({
      decision_applied: true,
      approval_request: { ...PENDING_APPROVAL.approval_request, status: "approved" },
    });
    const store = useApprovalStore();
    await store.loadPending();
    store.selectApproval(PENDING_APPROVAL.approval_request.id);

    const decided = await store.decide("approve", {
      expected_incident_version: 3,
      actor_id: "engineer-001",
      reason: "确认版本、风险和恢复判据",
    });

    expect(decided).toBe(true);
    expect(mocks.decideApprovalRequest).toHaveBeenCalledWith(
      "approval-001",
      "approve",
      {
        expected_incident_version: 3,
        actor_id: "engineer-001",
        reason: "确认版本、风险和恢复判据",
      },
    );
    expect(store.items).toEqual([]);
    expect(store.total).toBe(0);
    expect(store.selectedApproval).toBeNull();
    expect(store.actionMessage).toContain("尚未执行");
  });

  it("决定冲突时显示错误并刷新服务端队列", async () => {
    mocks.listPendingApprovals
      .mockResolvedValueOnce({ items: [PENDING_APPROVAL], total: 1 })
      .mockResolvedValueOnce({ items: [], total: 0 });
    mocks.decideApprovalRequest.mockRejectedValue(new Error("Incident 版本已经变化"));
    const store = useApprovalStore();
    await store.loadPending();
    store.selectApproval(PENDING_APPROVAL.approval_request.id);

    const decided = await store.decide("reject", {
      expected_incident_version: 3,
      actor_id: "engineer-001",
      reason: "证据不足",
    });

    expect(decided).toBe(false);
    expect(store.actionError).toBe("Incident 版本已经变化");
    expect(mocks.listPendingApprovals).toHaveBeenCalledTimes(2);
    expect(store.selectedApproval).toBeNull();
  });
});
