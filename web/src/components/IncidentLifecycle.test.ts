import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";

import IncidentLifecycle from "./IncidentLifecycle.vue";
import type { IncidentWorkroomSnapshot } from "../types/workroom";

const SNAPSHOT: IncidentWorkroomSnapshot = {
  incident_id: "incident-001",
  incident_version: 5,
  investigation: {
    id: "run-001",
    mode: "multi_agent",
    status: "succeeded",
    prompt_version: "multi-agent-parallel-v1",
    model_name: "fake-model",
    error_code: null,
    error_message: null,
    created_at: "2026-01-01T08:00:00Z",
    started_at: "2026-01-01T08:00:01Z",
    completed_at: "2026-01-01T08:00:02Z",
    updated_at: "2026-01-01T08:00:02Z",
  },
  evidence_board: {
    evidence_count: 3,
    specialist_report_count: 3,
    primary_hypothesis: {
      id: "hypothesis-001",
      root_cause: "release_500_regression",
      statement: "新版本导致接口大量返回 500",
      confidence: 0.96,
      status: "supported",
      challenge_summary: "三类证据一致",
      rule_version: "hypothesis-challenge-v1",
    },
  },
  action_plan: null,
  approval_request: null,
  operation_execution: null,
  verification_run: null,
};

describe("Incident 调查与处置链路", () => {
  it("展示调查证据摘要和未开始的后续阶段", () => {
    const wrapper = mount(IncidentLifecycle, {
      props: { snapshot: SNAPSHOT, loading: false, error: null },
    });

    expect(wrapper.text()).toContain("multi_agent");
    expect(wrapper.text()).toContain("3 条证据 · 3 份报告");
    expect(wrapper.text()).toContain("新版本导致接口大量返回 500");
    expect(wrapper.text()).toContain("尚未生成计划");
    expect(wrapper.findAll(".stage-card")).toHaveLength(6);
  });
});
