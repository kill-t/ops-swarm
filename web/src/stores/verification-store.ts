import { defineStore } from "pinia";
import { computed, ref } from "vue";

import {
  createVerificationRun,
  getVerificationRun,
  getVerificationWorkItem,
  listVerificationCandidates,
} from "../services/api";
import type {
  VerificationCandidate,
  VerificationRun,
  VerificationWorkItem,
} from "../types/verifications";

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "发生未知错误";
}

export const useVerificationStore = defineStore("verifications", () => {
  const items = ref<VerificationCandidate[]>([]);
  const total = ref(0);
  const selectedExecutionId = ref<string | null>(null);
  const observedCandidate = ref<VerificationCandidate | null>(null);
  const observedRun = ref<VerificationRun | null>(null);
  const observedWorkItem = ref<VerificationWorkItem | null>(null);
  const listLoading = ref(false);
  const creating = ref(false);
  const progressLoading = ref(false);
  const listError = ref<string | null>(null);
  const actionError = ref<string | null>(null);
  const actionMessage = ref<string | null>(null);
  let listGeneration = 0;
  let actionGeneration = 0;

  const selectedCandidate = computed(
    () =>
      items.value.find(
        (item) => item.operation_execution.id === selectedExecutionId.value,
      ) ?? null,
  );

  function syncCandidate(candidate: VerificationCandidate): void {
    observedCandidate.value = candidate;
    observedRun.value = candidate.verification_run;
    observedWorkItem.value = candidate.work_item;
  }

  async function loadCandidates(): Promise<void> {
    const generation = ++listGeneration;
    listLoading.value = true;
    listError.value = null;
    try {
      const page = await listVerificationCandidates();
      if (generation !== listGeneration) return;
      items.value = page.items;
      total.value = page.total;
      const selected = page.items.find(
        (item) => item.operation_execution.id === selectedExecutionId.value,
      );
      if (selected !== undefined) syncCandidate(selected);
      else if (selectedExecutionId.value !== null) selectedExecutionId.value = null;
    } catch (error) {
      if (generation === listGeneration) listError.value = errorMessage(error);
    } finally {
      if (generation === listGeneration) listLoading.value = false;
    }
  }

  function selectCandidate(executionId: string | null): void {
    selectedExecutionId.value = executionId;
    const candidate = items.value.find(
      (item) => item.operation_execution.id === executionId,
    );
    if (candidate !== undefined) syncCandidate(candidate);
  }

  async function createRun(): Promise<boolean> {
    const candidate = selectedCandidate.value;
    if (candidate === null || creating.value) return false;
    if (candidate.verification_run !== null) {
      syncCandidate(candidate);
      actionMessage.value = "该操作已有恢复验证，已读取原运行。";
      return true;
    }

    const generation = ++actionGeneration;
    creating.value = true;
    actionError.value = null;
    actionMessage.value = null;
    try {
      const response = await createVerificationRun(candidate.operation_execution.id, {
        expected_incident_version: candidate.incident.version,
      });
      if (generation !== actionGeneration) return false;
      observedRun.value = response.verification_run;
      observedWorkItem.value = await getVerificationWorkItem(response.verification_run.id);
      actionMessage.value = response.created
        ? "恢复验证已创建，Worker 将按冻结规则自动采样。"
        : "该操作已有恢复验证，已读取原运行。";
      await loadCandidates();
      return true;
    } catch (error) {
      if (generation === actionGeneration) {
        actionError.value = errorMessage(error);
        await loadCandidates();
      }
      return false;
    } finally {
      if (generation === actionGeneration) creating.value = false;
    }
  }

  async function refreshProgress(): Promise<boolean> {
    const runId = observedRun.value?.id;
    if (runId === undefined || progressLoading.value) return false;
    progressLoading.value = true;
    actionError.value = null;
    try {
      const [run, workItem] = await Promise.all([
        getVerificationRun(runId),
        getVerificationWorkItem(runId),
      ]);
      observedRun.value = run;
      observedWorkItem.value = workItem;
      if (run.status !== "running") {
        actionMessage.value =
          run.status === "passed"
            ? "恢复验证通过，Incident 已进入 resolved。"
            : "恢复验证失败，Incident 已返回 investigating。";
        await loadCandidates();
      }
      return true;
    } catch (error) {
      actionError.value = errorMessage(error);
      return false;
    } finally {
      progressLoading.value = false;
    }
  }

  function clearSelection(): void {
    actionGeneration += 1;
    selectedExecutionId.value = null;
    observedRun.value = null;
    observedWorkItem.value = null;
    observedCandidate.value = null;
    creating.value = false;
    progressLoading.value = false;
    actionError.value = null;
    actionMessage.value = null;
  }

  return {
    items,
    total,
    selectedExecutionId,
    selectedCandidate,
    observedCandidate,
    observedRun,
    observedWorkItem,
    listLoading,
    creating,
    progressLoading,
    listError,
    actionError,
    actionMessage,
    loadCandidates,
    selectCandidate,
    createRun,
    refreshProgress,
    clearSelection,
  };
});
