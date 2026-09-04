import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";

import {
  READY_VERIFICATION_CANDIDATE,
  RUNNING_VERIFICATION_CANDIDATE,
  VERIFICATION_RUN,
  VERIFICATION_WORK_ITEM,
} from "../test-support/verification-fixtures";
import VerificationCandidateDetail from "./VerificationCandidateDetail.vue";

const routerStub = { template: "<a><slot /></a>" };

describe("恢复验证详情", () => {
  it("明确确认后只发出创建验证事件", async () => {
    const wrapper = mount(VerificationCandidateDetail, {
      props: {
        candidate: READY_VERIFICATION_CANDIDATE,
        run: null,
        workItem: null,
        creating: false,
        progressLoading: false,
        error: null,
      },
      global: { stubs: { RouterLink: routerStub } },
    });
    const button = wrapper.get("button.verify");

    expect(wrapper.text()).toContain("只读自动验证");
    expect(wrapper.text()).toContain("http_5xx_ratio less_than 0.01");
    expect(wrapper.text()).toContain("3 个成功样本，覆盖 60 秒观察窗口");
    expect(button.attributes("disabled")).toBeDefined();

    await wrapper.get('input[type="checkbox"]').setValue(true);
    await button.trigger("click");

    expect(wrapper.emitted("create")?.length).toBe(1);
  });

  it("只读展示 Worker 进度和不可变样本", async () => {
    const wrapper = mount(VerificationCandidateDetail, {
      props: {
        candidate: RUNNING_VERIFICATION_CANDIDATE,
        run: VERIFICATION_RUN,
        workItem: VERIFICATION_WORK_ITEM,
        creating: false,
        progressLoading: false,
        error: null,
      },
      global: { stubs: { RouterLink: routerStub } },
    });

    expect(wrapper.text()).toContain("成功样本 1 / 3");
    expect(wrapper.text()).toContain("下一样本");
    expect(wrapper.text()).toContain("#1");
    expect(wrapper.text()).toContain("PASSED");
    expect(wrapper.find('input[type="number"]').exists()).toBe(false);
    expect(wrapper.find("textarea").exists()).toBe(false);

    await wrapper.get("button.icon-button").trigger("click");
    expect(wrapper.emitted("refresh")?.length).toBe(1);
  });
});
