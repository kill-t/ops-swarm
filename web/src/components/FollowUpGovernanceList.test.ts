import { mount, RouterLinkStub } from "@vue/test-utils";
import { describe, expect, it } from "vitest";

import { REPORT_ACTION_ITEM_GOVERNANCE_ENTRY } from "../test-support/report-action-item-fixtures";
import FollowUpGovernanceList from "./FollowUpGovernanceList.vue";

function mountList(overrides: Record<string, unknown> = {}) {
  return mount(FollowUpGovernanceList, {
    props: {
      items: [REPORT_ACTION_ITEM_GOVERNANCE_ENTRY],
      total: 1,
      asOf: "2026-10-02T08:00:00Z",
      loading: false,
      error: null,
      ...overrides,
    },
    global: { stubs: { RouterLink: RouterLinkStub } },
  });
}

describe("跨事故改进行动项治理列表", () => {
  it("展示服务器逾期判定、事故上下文和返回入口", () => {
    const wrapper = mountList();

    expect(wrapper.text()).toContain("未完成改进工作");
    expect(wrapper.text()).toContain("已逾期");
    expect(wrapper.text()).toContain("sre-001");
    expect(wrapper.text()).toContain("INC-000001");
    expect(wrapper.text()).toContain("订单接口大量返回 500");
    expect(wrapper.getComponent(RouterLinkStub).props("to")).toEqual({
      name: "incidents",
      params: { incidentId: "incident-001" },
    });
  });

  it("空结果和刷新保持只读语义", async () => {
    const wrapper = mountList({ items: [], total: 0, asOf: null });

    expect(wrapper.text()).toContain("没有未完成行动项");
    await wrapper.get("button.icon-button").trigger("click");
    expect(wrapper.emitted("refresh")).toHaveLength(1);
    expect(wrapper.find("button.complete-follow-up").exists()).toBe(false);
  });
});
