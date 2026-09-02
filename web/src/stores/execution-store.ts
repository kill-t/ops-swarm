import { defineStore } from "pinia";
import { computed, ref } from "vue";

import { executeActionStep, listExecutionCandidates } from "../services/api";
import type {
  ExecuteOperationResponse,
  ExecutionCandidate,
} from "../types/executions";

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "发生未知错误";
}

export function executionIdempotencyKey(approvalRequestId: string, stepId: string): string {
  return `ui-exec:${approvalRequestId}:${stepId}`;
}

export const useExecutionStore = defineStore("executions", () => {
  const items = ref<ExecutionCandidate[]>([]);
  const total = ref(0);
  const selectedPlanId = ref<string | null>(null);
  const listLoading = ref(false);
  const executingStepId = ref<string | null>(null);
  const listError = ref<string | null>(null);
  const actionError = ref<string | null>(null);
  const actionMessage = ref<string | null>(null);
  const lastOutcome = ref<ExecuteOperationResponse | null>(null);
  let listGeneration = 0;
  let actionGeneration = 0;

  const selectedCandidate = computed(
    () => items.value.find((item) => item.action_plan.id === selectedPlanId.value) ?? null,
  );

  async function loadCandidates(): Promise<void> {
    const generation = ++listGeneration;
    listLoading.value = true;
    listError.value = null;
    try {
      const page = await listExecutionCandidates();
      if (generation !== listGeneration) return;
      items.value = page.items;
      total.value = page.total;
      if (
        selectedPlanId.value !== null &&
        !page.items.some((item) => item.action_plan.id === selectedPlanId.value)
      ) {
        selectedPlanId.value = null;
      }
    } catch (error) {
      if (generation === listGeneration) listError.value = errorMessage(error);
    } finally {
      if (generation === listGeneration) listLoading.value = false;
    }
  }

  function selectCandidate(planId: string | null): void {
    selectedPlanId.value = planId;
  }

  async function execute(stepId: string): Promise<boolean> {
    const candidate = selectedCandidate.value;
    if (candidate === null || executingStepId.value !== null) return false;
    const step = candidate.action_plan.steps.find((item) => item.id === stepId);
    if (step === undefined) return false;
    const active = candidate.active_executions.find((item) => item.action_step_id === stepId);
    if (active !== undefined) {
      actionError.value = "该步骤已有运行中的操作，请等待执行完成后刷新。";
      return false;
    }

    const generation = ++actionGeneration;
    executingStepId.value = stepId;
    actionError.value = null;
    actionMessage.value = null;
    lastOutcome.value = null;
    try {
      const response = await executeActionStep(stepId, {
        idempotency_key: executionIdempotencyKey(candidate.approval_request.id, stepId),
        expected_incident_version: candidate.incident.version,
      });
      if (generation !== actionGeneration) return false;
      lastOutcome.value = response;
      actionMessage.value =
        response.operation_execution.status === "succeeded"
          ? "白名单动作执行成功；Incident 已进入恢复验证阶段。"
          : "操作正在执行，刷新后可读取同一幂等记录。";
      selectedPlanId.value = null;
      await loadCandidates();
      return true;
    } catch (error) {
      if (generation === actionGeneration) {
        actionError.value = errorMessage(error);
        await loadCandidates();
      }
      return false;
    } finally {
      if (generation === actionGeneration) executingStepId.value = null;
    }
  }

  function clearSelection(): void {
    actionGeneration += 1;
    selectedPlanId.value = null;
    executingStepId.value = null;
    actionError.value = null;
    actionMessage.value = null;
    lastOutcome.value = null;
  }

  return {
    items,
    total,
    selectedPlanId,
    selectedCandidate,
    listLoading,
    executingStepId,
    listError,
    actionError,
    actionMessage,
    lastOutcome,
    loadCandidates,
    selectCandidate,
    execute,
    clearSelection,
  };
});
