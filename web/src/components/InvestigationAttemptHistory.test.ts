import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";

import InvestigationAttemptHistory from "./InvestigationAttemptHistory.vue";
import type { InvestigationAttempt } from "../types/investigation-attempts";

const ATTEMPTS: InvestigationAttempt[] = [
  {
    id: "attempt-001",
    work_item_id: "work-001",
    attempt_number: 1,
    status: "failed",
    worker_id: "live-worker",
    model_name: "deepseek-v4-flash",
    pricing: {
      input_cost_per_million_usd: "1.500000",
      output_cost_per_million_usd: "4.500000",
    },
    usage: {
      model_request_count: 2,
      usage_complete: false,
      input_tokens: 120,
      output_tokens: 30,
      total_tokens: 150,
      estimated_cost_usd: null,
    },
    started_at: "2026-01-01T08:00:00Z",
    lease_expires_at: "2026-01-01T08:02:00Z",
    finished_at: "2026-01-01T08:00:01Z",
    duration_ms: 1500,
    will_retry: true,
    error: {
      error_code: "model_request_error",
      message: "模型请求未完成。",
    },
  },
  {
    id: "attempt-002",
    work_item_id: "work-001",
    attempt_number: 2,
    status: "succeeded",
    worker_id: "live-worker",
    model_name: "deepseek-v4-flash",
    pricing: {
      input_cost_per_million_usd: "1.500000",
      output_cost_per_million_usd: "4.500000",
    },
    usage: {
      model_request_count: 1,
      usage_complete: true,
      input_tokens: 500,
      output_tokens: 100,
      total_tokens: 600,
      estimated_cost_usd: "0.001200",
    },
    started_at: "2026-01-01T08:00:02Z",
    lease_expires_at: "2026-01-01T08:02:02Z",
    finished_at: "2026-01-01T08:00:03Z",
    duration_ms: 1000,
    will_retry: false,
    error: null,
  },
];

describe("调查尝试历史", () => {
  it("区分失败重试、用量不完整和成功费用", () => {
    const wrapper = mount(InvestigationAttemptHistory, {
      props: { attempts: ATTEMPTS, loading: false, error: null },
    });

    expect(wrapper.findAll(".attempt-card")).toHaveLength(2);
    expect(wrapper.text()).toContain("ATTEMPT 01");
    expect(wrapper.text()).toContain("失败");
    expect(wrapper.text()).toContain("供应商只返回了部分模型用量，费用未知");
    expect(wrapper.text()).toContain("model_request_error");
    expect(wrapper.text()).toContain("将重试");
    expect(wrapper.text()).not.toContain("不再重试");
    expect(wrapper.text()).toContain("$0.001200");
    expect(wrapper.text()).toContain("600");
  });

  it("为尚未领取的调查展示明确空状态", () => {
    const wrapper = mount(InvestigationAttemptHistory, {
      props: { attempts: [], loading: false, error: null },
    });

    expect(wrapper.text()).toContain("当前调查尚未被 Worker 领取");
  });

  it("把运行中尝试显示为等待结算而不是零费用", () => {
    const runningAttempt: InvestigationAttempt = {
      ...ATTEMPTS[0]!,
      id: "attempt-running",
      status: "running",
      usage: {
        model_request_count: 0,
        usage_complete: false,
        input_tokens: 0,
        output_tokens: 0,
        total_tokens: 0,
        estimated_cost_usd: null,
      },
      duration_ms: null,
      finished_at: null,
      will_retry: null,
      error: null,
    };
    const wrapper = mount(InvestigationAttemptHistory, {
      props: { attempts: [runningAttempt], loading: false, error: null },
    });

    expect(wrapper.text()).toContain("等待结算");
    expect(wrapper.text()).toContain("等待本次尝试结算用量");
    expect(wrapper.text()).not.toContain("$0.000000");
  });
});
