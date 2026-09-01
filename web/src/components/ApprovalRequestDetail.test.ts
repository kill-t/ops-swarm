import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";

import { PENDING_APPROVAL } from "../test-support/approval-fixtures";
import ApprovalRequestDetail from "./ApprovalRequestDetail.vue";

describe("审批请求详情", () => {
  it("要求明确复核后才提交带乐观锁版本的决定", async () => {
    const wrapper = mount(ApprovalRequestDetail, {
      props: { approval: PENDING_APPROVAL, loading: false, error: null },
      global: {
        stubs: { RouterLink: { template: "<a><slot /></a>" } },
      },
    });
    const approveButton = wrapper.get("button.approve");

    expect(wrapper.text()).toContain("2026.01.01-rc1 → stable");
    expect(wrapper.text()).toContain("批准不会执行动作");
    expect(approveButton.attributes("disabled")).toBeDefined();

    await wrapper.get("textarea").setValue("确认版本、动作参数和恢复判据");
    await wrapper.get('input[type="checkbox"]').setValue(true);
    expect(approveButton.attributes("disabled")).toBeUndefined();
    await approveButton.trigger("click");

    expect(wrapper.emitted("decide")?.[0]).toEqual([
      "approve",
      {
        expected_incident_version: 3,
        actor_id: "engineer-001",
        reason: "确认版本、动作参数和恢复判据",
      },
    ]);
  });

  it("服务端错误保持可见且不会修改计划展示", () => {
    const wrapper = mount(ApprovalRequestDetail, {
      props: {
        approval: PENDING_APPROVAL,
        loading: false,
        error: "Incident 版本已经变化，请刷新后重新核对。",
      },
      global: {
        stubs: { RouterLink: { template: "<a><slot /></a>" } },
      },
    });

    expect(wrapper.get('[role="alert"]').text()).toContain("版本已经变化");
    expect(wrapper.text()).toContain(PENDING_APPROVAL.action_plan.summary);
  });
});
