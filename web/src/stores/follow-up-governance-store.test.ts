import { createPinia, setActivePinia } from "pinia";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { REPORT_ACTION_ITEM_GOVERNANCE_ENTRY } from "../test-support/report-action-item-fixtures";
import type { ReportActionItemGovernancePage } from "../types/report-action-items";
import { useFollowUpGovernanceStore } from "./follow-up-governance-store";

const mocks = vi.hoisted(() => ({
  listOpenReportActionItems: vi.fn(),
}));

vi.mock("../services/api", () => ({
  listOpenReportActionItems: mocks.listOpenReportActionItems,
}));

const PAGE: ReportActionItemGovernancePage = {
  items: [REPORT_ACTION_ITEM_GOVERNANCE_ENTRY],
  total: 1,
  as_of: "2026-10-02T08:00:00Z",
  owner_id: "sre-001",
  overdue_only: true,
};

describe("跨事故改进行动项治理状态仓库", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    mocks.listOpenReportActionItems.mockReset();
  });

  it("规范化筛选并保存服务端观察时间", async () => {
    mocks.listOpenReportActionItems.mockResolvedValue(PAGE);
    const store = useFollowUpGovernanceStore();

    await expect(store.load(" sre-001 ", true)).resolves.toBe(true);

    expect(mocks.listOpenReportActionItems).toHaveBeenCalledWith({
      ownerId: "sre-001",
      overdueOnly: true,
    });
    expect(store.items).toEqual([REPORT_ACTION_ITEM_GOVERNANCE_ENTRY]);
    expect(store.total).toBe(1);
    expect(store.asOf).toBe("2026-10-02T08:00:00Z");
    expect(store.error).toBeNull();
  });

  it("拒绝与当前筛选条件不一致的服务端响应", async () => {
    mocks.listOpenReportActionItems.mockResolvedValue({
      ...PAGE,
      owner_id: "sre-002",
    });
    const store = useFollowUpGovernanceStore();

    await expect(store.load("sre-001", true)).resolves.toBe(false);

    expect(store.items).toEqual([]);
    expect(store.error).toContain("筛选条件不一致");
  });

  it("后发筛选请求不会被旧响应覆盖", async () => {
    const firstRequest: {
      resolve?: (value: ReportActionItemGovernancePage) => void;
    } = {};
    mocks.listOpenReportActionItems
      .mockImplementationOnce(
        () =>
          new Promise<ReportActionItemGovernancePage>((resolve) => {
            firstRequest.resolve = resolve;
          }),
      )
      .mockResolvedValueOnce({
        items: [],
        total: 0,
        as_of: "2026-10-02T08:01:00Z",
        owner_id: "sre-002",
        overdue_only: false,
      });
    const store = useFollowUpGovernanceStore();

    const first = store.load("sre-001", true);
    await store.load("sre-002", false);
    if (firstRequest.resolve === undefined) throw new Error("第一个请求没有开始");
    firstRequest.resolve(PAGE);
    await first;

    expect(store.ownerId).toBe("sre-002");
    expect(store.items).toEqual([]);
    expect(store.asOf).toBe("2026-10-02T08:01:00Z");
  });
});
