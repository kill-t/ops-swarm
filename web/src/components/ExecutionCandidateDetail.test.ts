import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";

import { EXECUTION_CANDIDATE, SUCCEEDED_EXECUTION } from "../test-support/execution-fixtures";
import ExecutionCandidateDetail from "./ExecutionCandidateDetail.vue";

describe("受控执行详情", () => {
  it("明确确认后只提交数据库步骤 ID", async () => {
    const wrapper = mount(ExecutionCandidateDetail, {
      props: { candidate: EXECUTION_CANDIDATE, executingStepId: null, error: null },
      global: {
        stubs: { RouterLink: { template: "<a><slot /></a>" } },
      },
    });
    const executeButton = wrapper.get("button.execute");

    expect(wrapper.text()).toContain("将修改 LOCAL FAULT LAB");
    expect(wrapper.text()).toContain("2026.01.01-rc1 → stable");
    expect(wrapper.text()).toContain("ui-exec:approval-001:step-001");
    expect(executeButton.attributes("disabled")).toBeDefined();

    await wrapper.get('input[type="checkbox"]').setValue(true);
    expect(executeButton.attributes("disabled")).toBeUndefined();
    await executeButton.trigger("click");

    expect(wrapper.emitted("execute")?.[0]).toEqual(["step-001"]);
  });

  it("已有运行记录时展示租约并阻止重复操作", () => {
    const candidate = {
      ...EXECUTION_CANDIDATE,
      active_executions: [
        {
          ...SUCCEEDED_EXECUTION,
          status: "running" as const,
          idempotency_key: "cli-exec-running-001",
          locked_until: "2099-09-01T09:07:00Z",
          result: null,
          completed_at: null,
        },
      ],
    };
    const wrapper = mount(ExecutionCandidateDetail, {
      props: { candidate, executingStepId: null, error: null },
      global: {
        stubs: { RouterLink: { template: "<a><slot /></a>" } },
      },
    });

    expect(wrapper.text()).toContain("操作正在执行");
    expect(wrapper.text()).toContain("local_fault_lab_release_500_v1");
    expect(wrapper.text()).toContain("cli-exec-running-001");
    expect(wrapper.get("button.execute").attributes("disabled")).toBeDefined();
  });
});
