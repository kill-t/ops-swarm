import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";

import { EVALUATION_RUN } from "../test-support/evaluation-fixtures";
import EvaluationRunDetail from "./EvaluationRunDetail.vue";

describe("评测运行详情", () => {
  it("展示解释边界、聚合指标和逐场景证据", () => {
    const wrapper = mount(EvaluationRunDetail, {
      props: { run: EVALUATION_RUN, loading: false, error: null },
    });

    expect(wrapper.text()).toContain("冻结假模型契约评测");
    expect(wrapper.text()).toContain("根因 Top-1");
    expect(wrapper.text()).toContain("差值 = 多 Agent − 单 Agent");
    expect(wrapper.text()).toContain("release-500-baseline-v1");
    expect(wrapper.text()).toContain("新版本回归");
    expect(wrapper.text()).toContain("log-001 · metric-001 · change-001");
    expect(wrapper.findAll(".metric-row")).toHaveLength(7);
    expect(wrapper.findAll(".scenario-card")).toHaveLength(1);
    expect(wrapper.findAll(".mode-result-card")).toHaveLength(2);
  });
});
