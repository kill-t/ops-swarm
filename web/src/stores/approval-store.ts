import { defineStore } from "pinia";
import { computed, ref } from "vue";

import { decideApprovalRequest, listPendingApprovals } from "../services/api";
import type {
  ApprovalDecision,
  ApprovalDecisionInput,
  PendingApproval,
} from "../types/approvals";

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "发生未知错误";
}

export const useApprovalStore = defineStore("approvals", () => {
  const items = ref<PendingApproval[]>([]);
  const total = ref(0);
  const selectedRequestId = ref<string | null>(null);
  const listLoading = ref(false);
  const actionLoading = ref(false);
  const listError = ref<string | null>(null);
  const actionError = ref<string | null>(null);
  const actionMessage = ref<string | null>(null);
  let listGeneration = 0;
  let actionGeneration = 0;

  const selectedApproval = computed(
    () =>
      items.value.find(
        (item) => item.approval_request.id === selectedRequestId.value,
      ) ?? null,
  );

  async function loadPending(): Promise<void> {
    const generation = ++listGeneration;
    listLoading.value = true;
    listError.value = null;
    try {
      const page = await listPendingApprovals();
      if (generation !== listGeneration) {
        return;
      }
      items.value = page.items;
      total.value = page.total;
      if (
        selectedRequestId.value !== null &&
        !page.items.some((item) => item.approval_request.id === selectedRequestId.value)
      ) {
        selectedRequestId.value = null;
      }
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

  function selectApproval(requestId: string | null): void {
    selectedRequestId.value = requestId;
    actionError.value = null;
  }

  async function decide(
    decision: ApprovalDecision,
    input: ApprovalDecisionInput,
  ): Promise<boolean> {
    const selected = selectedApproval.value;
    if (selected === null || actionLoading.value) {
      return false;
    }
    const generation = ++actionGeneration;
    actionLoading.value = true;
    actionError.value = null;
    actionMessage.value = null;
    try {
      const response = await decideApprovalRequest(
        selected.approval_request.id,
        decision,
        input,
      );
      if (generation !== actionGeneration) {
        return false;
      }
      actionMessage.value = response.decision_applied
        ? decision === "approve"
          ? "计划已批准；尚未执行任何环境操作。"
          : "计划已拒绝，Incident 已返回调查阶段。"
        : "该决定已处理，本次请求没有重复修改状态。";
      selectedRequestId.value = null;
      await loadPending();
      return true;
    } catch (error) {
      if (generation === actionGeneration) {
        actionError.value = errorMessage(error);
        await loadPending();
      }
      return false;
    } finally {
      if (generation === actionGeneration) {
        actionLoading.value = false;
      }
    }
  }

  function clearSelection(): void {
    actionGeneration += 1;
    selectedRequestId.value = null;
    actionLoading.value = false;
    actionError.value = null;
    actionMessage.value = null;
  }

  return {
    items,
    total,
    selectedRequestId,
    selectedApproval,
    listLoading,
    actionLoading,
    listError,
    actionError,
    actionMessage,
    loadPending,
    selectApproval,
    decide,
    clearSelection,
  };
});
