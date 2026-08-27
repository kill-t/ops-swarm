import { defineStore } from "pinia";
import { computed, ref } from "vue";

import { getEvaluationRun, listEvaluationRuns } from "../services/api";
import type { EvaluationRun, EvaluationRunSummary } from "../types/evaluations";

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "发生未知错误";
}

export const useEvaluationStore = defineStore("evaluations", () => {
  const runs = ref<EvaluationRunSummary[]>([]);
  const selectedRun = ref<EvaluationRun | null>(null);
  const nextBeforeSequence = ref<number | null>(null);
  const listLoading = ref(false);
  const detailLoading = ref(false);
  const listError = ref<string | null>(null);
  const detailError = ref<string | null>(null);
  let listGeneration = 0;
  let selectionGeneration = 0;

  const selectedRunId = computed(() => selectedRun.value?.id ?? null);

  async function loadRuns(reset = true): Promise<void> {
    if (listLoading.value && !reset) {
      return;
    }
    const generation = ++listGeneration;
    const cursor = reset ? undefined : (nextBeforeSequence.value ?? undefined);
    if (!reset && cursor === undefined) {
      return;
    }
    listLoading.value = true;
    listError.value = null;
    try {
      const page = await listEvaluationRuns(cursor);
      if (generation !== listGeneration) {
        return;
      }
      if (reset) {
        runs.value = page.items;
      } else {
        const existingIds = new Set(runs.value.map((run) => run.id));
        runs.value = [...runs.value, ...page.items.filter((run) => !existingIds.has(run.id))];
      }
      nextBeforeSequence.value = page.next_before_sequence;
    } catch (error) {
      if (generation === listGeneration) {
        listError.value = errorMessage(error);
      }
    } finally {
      if (generation === listGeneration) {
        listLoading.value = false;
      }
    }
  }

  async function selectRun(runId: string): Promise<void> {
    const generation = ++selectionGeneration;
    detailLoading.value = true;
    detailError.value = null;
    try {
      const run = await getEvaluationRun(runId);
      if (generation === selectionGeneration) {
        selectedRun.value = run;
      }
    } catch (error) {
      if (generation === selectionGeneration) {
        selectedRun.value = null;
        detailError.value = errorMessage(error);
      }
    } finally {
      if (generation === selectionGeneration) {
        detailLoading.value = false;
      }
    }
  }

  function clearSelection(): void {
    selectionGeneration += 1;
    selectedRun.value = null;
    detailLoading.value = false;
    detailError.value = null;
  }

  return {
    runs,
    selectedRun,
    selectedRunId,
    nextBeforeSequence,
    listLoading,
    detailLoading,
    listError,
    detailError,
    loadRuns,
    selectRun,
    clearSelection,
  };
});
