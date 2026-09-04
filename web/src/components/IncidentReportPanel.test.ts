import { mount } from "@vue/test-utils";
import { afterEach, describe, expect, it, vi } from "vitest";

import { INCIDENT_REPORT } from "../test-support/report-fixtures";
import type { Incident } from "../types/incidents";
import IncidentReportPanel from "./IncidentReportPanel.vue";

const CLOSED_INCIDENT: Incident = {
  id: "incident-001",
  display_number: 1,
  reference: "INC-000001",
  service_name: "order-service",
  title: "订单接口大量返回 500",
  severity: "p1",
  status: "closed",
  fingerprint: "order-service:http-500",
  version: 7,
  started_at: "2026-01-01T08:00:00Z",
  resolved_at: "2026-01-01T08:08:00Z",
  closed_at: "2026-01-01T08:09:00Z",
  created_at: "2026-01-01T08:00:00Z",
  updated_at: "2026-01-01T08:09:00Z",
};

describe("Incident 事故复盘报告面板", () => {
  afterEach(() => vi.restoreAllMocks());

  it("没有报告时只收集审计操作者并请求服务端生成", async () => {
    const wrapper = mount(IncidentReportPanel, {
      props: {
        incident: CLOSED_INCIDENT,
        report: null,
        loading: false,
        error: null,
        message: null,
      },
    });

    expect(wrapper.text()).toContain("只冻结已提交事实");
    expect(wrapper.find("textarea").exists()).toBe(false);
    await wrapper.get('input[type="text"]').setValue(" report-owner ");
    await wrapper.get("button.generate-report").trigger("click");

    expect(wrapper.emitted("generate")?.[0]).toEqual(["report-owner"]);
  });

  it("展示冻结的根因、处置、验证、时间线和内容摘要", () => {
    const wrapper = mount(IncidentReportPanel, {
      props: {
        incident: CLOSED_INCIDENT,
        report: INCIDENT_REPORT,
        loading: false,
        error: null,
        message: "事故复盘报告已冻结。",
      },
    });

    expect(wrapper.text()).toContain("INC-000001 · 订单接口大量返回 500");
    expect(wrapper.text()).toContain("新版本导致订单接口大量返回 500");
    expect(wrapper.text()).toContain("回滚到最近稳定版本");
    expect(wrapper.text()).toContain("3 / 3，共保存 3 个样本");
    expect(wrapper.text()).toContain("恢复结果已经人工复核");
    expect(wrapper.text()).toContain("incident-report.v1");
    expect(wrapper.text()).toContain("a".repeat(64));
  });

  it("打印按钮调用浏览器打印能力，非 closed 状态不渲染", async () => {
    const print = vi.spyOn(window, "print").mockImplementation(() => undefined);
    const wrapper = mount(IncidentReportPanel, {
      props: {
        incident: CLOSED_INCIDENT,
        report: INCIDENT_REPORT,
        loading: false,
        error: null,
        message: null,
      },
    });

    await wrapper.get("button.print-report").trigger("click");
    expect(print).toHaveBeenCalledOnce();

    await wrapper.setProps({ incident: { ...CLOSED_INCIDENT, status: "resolved" } });
    expect(wrapper.html()).toBe("<!--v-if-->");
  });
});
