import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";

import type { Incident } from "../types/incidents";
import IncidentClosurePanel from "./IncidentClosurePanel.vue";

const RESOLVED_INCIDENT: Incident = {
  id: "incident-001",
  display_number: 1,
  reference: "INC-000001",
  service_name: "order-service",
  title: "订单接口大量返回 500",
  severity: "p1",
  status: "resolved",
  fingerprint: "order-service:http-500",
  version: 6,
  started_at: "2099-09-01T09:00:00Z",
  resolved_at: "2099-09-01T09:08:00Z",
  closed_at: null,
  created_at: "2099-09-01T09:00:00Z",
  updated_at: "2099-09-01T09:08:00Z",
};

describe("Incident 人工关闭面板", () => {
  it("填写审计原因并二次确认后只提交审计字段", async () => {
    const wrapper = mount(IncidentClosurePanel, {
      props: { incident: RESOLVED_INCIDENT, loading: false, error: null, message: null },
    });
    const button = wrapper.get("button.close-incident");

    expect(wrapper.text()).toContain("恢复不等于关闭");
    expect(wrapper.text()).toContain("v6");
    expect(button.attributes("disabled")).toBeDefined();

    await wrapper.get("textarea").setValue("恢复验证窗口通过，观察期内未再出现错误。");
    await wrapper.get('input[type="checkbox"]').setValue(true);
    await button.trigger("click");

    expect(wrapper.emitted("close")?.[0]).toEqual([
      {
        actor_id: "engineer-001",
        reason: "恢复验证窗口通过，观察期内未再出现错误。",
      },
    ]);
  });

  it("closed 终态只展示结果而不再提供关闭控件", () => {
    const wrapper = mount(IncidentClosurePanel, {
      props: {
        incident: {
          ...RESOLVED_INCIDENT,
          status: "closed",
          version: 7,
          closed_at: "2099-09-01T09:10:00Z",
        },
        loading: false,
        error: null,
        message: "Incident 已正式关闭，历史审计记录保持只读。",
      },
    });

    expect(wrapper.text()).toContain("事故已关闭");
    expect(wrapper.text()).toContain("后续同指纹告警会创建新的 Incident");
    expect(wrapper.find("button.close-incident").exists()).toBe(false);
    expect(wrapper.find("textarea").exists()).toBe(false);
  });

  it("非 resolved 状态不渲染关闭入口", () => {
    const wrapper = mount(IncidentClosurePanel, {
      props: {
        incident: { ...RESOLVED_INCIDENT, status: "investigating", resolved_at: null },
        loading: false,
        error: null,
        message: null,
      },
    });

    expect(wrapper.html()).toBe("<!--v-if-->");
  });
});
