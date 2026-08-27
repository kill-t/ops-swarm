import { createPinia, setActivePinia } from "pinia";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  EVALUATION_RUN,
  EVALUATION_RUN_SUMMARY,
} from "../test-support/evaluation-fixtures";
import type { EvaluationRun } from "../types/evaluations";
import { useEvaluationStore } from "./evaluation-store";

const mocks = vi.hoisted(() => ({
  listEvaluationRuns: vi.fn(),
  getEvaluationRun: vi.fn(),
}));

vi.mock("../services/api", () => ({
  listEvaluationRuns: mocks.listEvaluationRuns,
  getEvaluationRun: mocks.getEvaluationRun,
}));

describe("评测状态仓库", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    mocks.listEvaluationRuns.mockReset();
    mocks.getEvaluationRun.mockReset();
  });

  it("按服务端游标追加运行并过滤重复项", async () => {
    const older = { ...EVALUATION_RUN_SUMMARY, id: "evaluation-run-older", sequence: 1 };
    const latest = { ...EVALUATION_RUN_SUMMARY, id: "evaluation-run-latest", sequence: 2 };
    mocks.listEvaluationRuns
      .mockResolvedValueOnce({ items: [latest], next_before_sequence: 2 })
      .mockResolvedValueOnce({ items: [latest, older], next_before_sequence: null });
    const store = useEvaluationStore();

    await store.loadRuns();
    await store.loadRuns(false);

    expect(mocks.listEvaluationRuns).toHaveBeenNthCalledWith(1, undefined);
    expect(mocks.listEvaluationRuns).toHaveBeenNthCalledWith(2, 2);
    expect(store.runs.map((run) => run.id)).toEqual([latest.id, older.id]);
    expect(store.nextBeforeSequence).toBeNull();
  });

  it("快速切换时拒绝较早详情覆盖当前运行", async () => {
    const first = Promise.withResolvers<EvaluationRun>();
    const second = Promise.withResolvers<EvaluationRun>();
    const latest = { ...EVALUATION_RUN, id: "evaluation-run-latest", sequence: 2 };
    mocks.getEvaluationRun.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    const store = useEvaluationStore();

    const firstSelection = store.selectRun(EVALUATION_RUN.id);
    const secondSelection = store.selectRun(latest.id);
    second.resolve(latest);
    await secondSelection;
    first.resolve(EVALUATION_RUN);
    await firstSelection;

    expect(store.selectedRun?.id).toBe(latest.id);
    expect(store.detailLoading).toBe(false);
  });
});
