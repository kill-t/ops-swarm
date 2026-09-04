import { createPinia, setActivePinia } from "pinia";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiRequestError } from "../services/api";
import {
  COMPLETED_REPORT_ACTION_ITEM,
  OPEN_REPORT_ACTION_ITEM,
} from "../test-support/report-action-item-fixtures";
import type { ReportActionItemPage } from "../types/report-action-items";
import { useReportActionItemStore } from "./report-action-item-store";

const mocks = vi.hoisted(() => ({
  listReportActionItems: vi.fn(),
  createReportActionItem: vi.fn(),
  completeReportActionItem: vi.fn(),
}));

vi.mock("../services/api", () => ({
  ApiRequestError: class extends Error {
    readonly status: number;
    readonly errorCode: string | null;

    constructor(message: string, status: number, errorCode: string | null) {
      super(message);
      this.status = status;
      this.errorCode = errorCode;
    }
  },
  listReportActionItems: mocks.listReportActionItems,
  createReportActionItem: mocks.createReportActionItem,
  completeReportActionItem: mocks.completeReportActionItem,
}));

describe("复盘改进行动项状态仓库", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    mocks.listReportActionItems.mockReset();
    mocks.createReportActionItem.mockReset();
    mocks.completeReportActionItem.mockReset();
  });

  it("按待完成优先读取当前报告行动项", async () => {
    const historicalCompletedItem = {
      ...COMPLETED_REPORT_ACTION_ITEM,
      id: "action-item-002",
      idempotency_key: "follow-up-002",
    };
    mocks.listReportActionItems.mockResolvedValue({
      items: [historicalCompletedItem, OPEN_REPORT_ACTION_ITEM],
      total: 2,
    });
    const store = useReportActionItemStore();

    await store.selectReport("report-001");

    expect(mocks.listReportActionItems).toHaveBeenCalledWith("report-001");
    expect(store.items.map((item) => item.status)).toEqual(["open", "completed"]);
    expect(store.total).toBe(2);
    expect(store.error).toBeNull();
  });

  it("创建时规范化人工字段并合并服务端记录", async () => {
    mocks.listReportActionItems.mockResolvedValue({ items: [], total: 0 });
    mocks.createReportActionItem.mockResolvedValue({
      created: true,
      action_item: OPEN_REPORT_ACTION_ITEM,
    });
    const store = useReportActionItemStore();
    await store.selectReport("report-001");

    await expect(
      store.create({
        idempotency_key: " follow-up-001 ",
        title: " 补充 Redis 故障演练 ",
        description: " 把依赖恢复和验证步骤加入每月演练。 ",
        owner_id: " sre-001 ",
        due_at: "2026-10-01T08:00:00Z",
        actor_id: " engineer-001 ",
      }),
    ).resolves.toBe(true);

    expect(mocks.createReportActionItem).toHaveBeenCalledWith("report-001", {
      idempotency_key: "follow-up-001",
      title: "补充 Redis 故障演练",
      description: "把依赖恢复和验证步骤加入每月演练。",
      owner_id: "sre-001",
      due_at: "2026-10-01T08:00:00Z",
      actor_id: "engineer-001",
    });
    expect(store.items).toEqual([OPEN_REPORT_ACTION_ITEM]);
    expect(store.total).toBe(1);
    expect(store.message).toContain("已创建");
  });

  it("完成时从当前记录取得版本并保存完成响应", async () => {
    mocks.listReportActionItems.mockResolvedValue({
      items: [OPEN_REPORT_ACTION_ITEM],
      total: 1,
    });
    mocks.completeReportActionItem.mockResolvedValue({
      completion_applied: true,
      action_item: COMPLETED_REPORT_ACTION_ITEM,
    });
    const store = useReportActionItemStore();
    await store.selectReport("report-001");

    await expect(
      store.complete("action-item-001", " engineer-002 ", " 演练脚本和恢复检查已经合入。 "),
    ).resolves.toBe(true);

    expect(mocks.completeReportActionItem).toHaveBeenCalledWith("action-item-001", {
      expected_version: 1,
      actor_id: "engineer-002",
      completion_note: "演练脚本和恢复检查已经合入。",
    });
    expect(store.items).toEqual([COMPLETED_REPORT_ACTION_ITEM]);
    expect(store.message).toContain("完成审计不可修改");
  });

  it("版本冲突后刷新最新状态但保留原始错误", async () => {
    mocks.listReportActionItems
      .mockResolvedValueOnce({ items: [OPEN_REPORT_ACTION_ITEM], total: 1 })
      .mockResolvedValueOnce({ items: [COMPLETED_REPORT_ACTION_ITEM], total: 1 });
    mocks.completeReportActionItem.mockRejectedValue(
      new ApiRequestError("行动项版本已经变化", 409, "report_action_item_version_conflict"),
    );
    const store = useReportActionItemStore();
    await store.selectReport("report-001");

    await expect(
      store.complete("action-item-001", "engineer-002", "已经完成"),
    ).resolves.toBe(false);

    expect(mocks.listReportActionItems).toHaveBeenCalledTimes(2);
    expect(store.items).toEqual([COMPLETED_REPORT_ACTION_ITEM]);
    expect(store.error).toBe("行动项版本已经变化");
    expect(store.mutationLoading).toBe(false);
  });

  it("切换报告后丢弃前一个报告的迟到响应", async () => {
    const firstRequest: {
      resolve?: (value: ReportActionItemPage) => void;
    } = {};
    mocks.listReportActionItems
      .mockImplementationOnce(
        () =>
          new Promise<ReportActionItemPage>((resolve) => {
            firstRequest.resolve = resolve;
          }),
      )
      .mockResolvedValueOnce({ items: [], total: 0 });
    const store = useReportActionItemStore();

    const first = store.selectReport("report-001");
    await store.selectReport("report-002");
    if (firstRequest.resolve === undefined) throw new Error("第一个请求没有开始");
    firstRequest.resolve({ items: [OPEN_REPORT_ACTION_ITEM], total: 1 });
    await first;

    expect(store.selectedReportId).toBe("report-002");
    expect(store.items).toEqual([]);
    expect(store.total).toBe(0);
  });
});
