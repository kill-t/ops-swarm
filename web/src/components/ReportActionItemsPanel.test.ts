import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";

import {
  COMPLETED_REPORT_ACTION_ITEM,
  OPEN_REPORT_ACTION_ITEM,
} from "../test-support/report-action-item-fixtures";
import type { CreateReportActionItemInput } from "../types/report-action-items";
import ReportActionItemsPanel from "./ReportActionItemsPanel.vue";

const HISTORICAL_COMPLETED_ITEM = {
  ...COMPLETED_REPORT_ACTION_ITEM,
  id: "action-item-002",
  idempotency_key: "follow-up-002",
  title: "补充发布检查清单",
};

function mountPanel(items = [OPEN_REPORT_ACTION_ITEM, HISTORICAL_COMPLETED_ITEM]) {
  return mount(ReportActionItemsPanel, {
    props: {
      reportId: "report-001",
      items,
      total: items.length,
      loading: false,
      mutationLoading: false,
      error: null,
      message: null,
    },
  });
}

describe("复盘改进行动项面板", () => {
  it("区分待完成、已完成和不可变完成审计", () => {
    const wrapper = mountPanel();

    expect(wrapper.text()).toContain("报告保持冻结，行动项独立推进");
    expect(wrapper.text()).toContain("补充 Redis 故障演练");
    expect(wrapper.text()).toContain("待完成");
    expect(wrapper.text()).toContain("已完成");
    expect(wrapper.text()).toContain("演练脚本和恢复检查已经合入");
    expect(wrapper.findAll("button.complete-follow-up")).toHaveLength(1);
  });

  it("创建表单只提交结构化字段并在内容不变时复用幂等键", async () => {
    const wrapper = mountPanel([]);
    await wrapper.get('input[name="action-title"]').setValue(" 补充连接池演练 ");
    await wrapper
      .get('textarea[name="action-description"]')
      .setValue(" 固化配置回滚验证步骤 ");
    await wrapper.get('input[name="action-owner"]').setValue(" sre-002 ");
    await wrapper.get('input[name="action-due-at"]').setValue("2026-10-01T08:00");

    await wrapper.get("form.follow-up-create-form").trigger("submit");
    await wrapper.get("form.follow-up-create-form").trigger("submit");

    const first = wrapper.emitted("create")?.[0]?.[0] as CreateReportActionItemInput;
    const second = wrapper.emitted("create")?.[1]?.[0] as CreateReportActionItemInput;
    expect(first).toMatchObject({
      title: "补充连接池演练",
      description: "固化配置回滚验证步骤",
      owner_id: "sre-002",
      actor_id: "engineer-001",
    });
    expect(first.idempotency_key).toMatch(/^follow-up-/);
    expect(Number.isFinite(Date.parse(first.due_at))).toBe(true);
    expect(second.idempotency_key).toBe(first.idempotency_key);
  });

  it("完成动作只上送当前条目标识、操作者和说明", async () => {
    const wrapper = mountPanel([OPEN_REPORT_ACTION_ITEM]);
    await wrapper
      .get('input[name="completion-actor-action-item-001"]')
      .setValue(" engineer-009 ");
    await wrapper
      .get('input[name="completion-note-action-item-001"]')
      .setValue(" 已完成并由值班工程师复核 ");
    await wrapper.get("form.follow-up-completion-form").trigger("submit");

    expect(wrapper.emitted("complete")?.[0]).toEqual([
      "action-item-001",
      "engineer-009",
      "已完成并由值班工程师复核",
    ]);
  });
});
