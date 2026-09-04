import { defineStore } from "pinia";
import { ref } from "vue";

import {
  ApiRequestError,
  completeReportActionItem,
  createReportActionItem,
  listReportActionItems,
} from "../services/api";
import type {
  CreateReportActionItemInput,
  ReportActionItem,
} from "../types/report-action-items";

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "发生未知错误";
}

function orderedItems(items: ReportActionItem[]): ReportActionItem[] {
  return [...items].sort((left, right) => {
    if (left.status !== right.status) return left.status === "open" ? -1 : 1;
    const dueDifference = Date.parse(left.due_at) - Date.parse(right.due_at);
    if (dueDifference !== 0) return dueDifference;
    return left.id.localeCompare(right.id);
  });
}

function upsertItem(items: ReportActionItem[], incoming: ReportActionItem): ReportActionItem[] {
  const exists = items.some((item) => item.id === incoming.id);
  const merged = exists
    ? items.map((item) => (item.id === incoming.id ? incoming : item))
    : [...items, incoming];
  return orderedItems(merged);
}

function isCompletionConflict(error: unknown): boolean {
  return (
    error instanceof ApiRequestError &&
    ["report_action_item_version_conflict", "report_action_item_already_completed"].includes(
      error.errorCode ?? "",
    )
  );
}

export const useReportActionItemStore = defineStore("report-action-items", () => {
  const selectedReportId = ref<string | null>(null);
  const items = ref<ReportActionItem[]>([]);
  const total = ref(0);
  const loading = ref(false);
  const mutationLoading = ref(false);
  const error = ref<string | null>(null);
  const message = ref<string | null>(null);
  let selectionGeneration = 0;
  let loadRequestGeneration = 0;

  async function loadItems(
    reportId: string,
    selection: number,
    clearMessage: boolean,
  ): Promise<boolean> {
    const requestGeneration = ++loadRequestGeneration;
    loading.value = true;
    error.value = null;
    if (clearMessage) message.value = null;
    try {
      const page = await listReportActionItems(reportId);
      if (page.items.some((item) => item.report_id !== reportId)) {
        throw new Error("复盘改进行动项响应与当前报告不一致");
      }
      if (
        selection !== selectionGeneration ||
        requestGeneration !== loadRequestGeneration ||
        selectedReportId.value !== reportId
      ) {
        return false;
      }
      items.value = orderedItems(page.items);
      total.value = page.total;
      return true;
    } catch (caught) {
      if (
        selection === selectionGeneration &&
        requestGeneration === loadRequestGeneration &&
        selectedReportId.value === reportId
      ) {
        error.value = errorMessage(caught);
      }
      return false;
    } finally {
      if (
        selection === selectionGeneration &&
        requestGeneration === loadRequestGeneration &&
        selectedReportId.value === reportId
      ) {
        loading.value = false;
      }
    }
  }

  async function selectReport(reportId: string): Promise<void> {
    const selection = ++selectionGeneration;
    selectedReportId.value = reportId;
    items.value = [];
    total.value = 0;
    mutationLoading.value = false;
    error.value = null;
    message.value = null;
    loadRequestGeneration += 1;
    await loadItems(reportId, selection, false);
  }

  async function refresh(): Promise<boolean> {
    const reportId = selectedReportId.value;
    if (reportId === null || mutationLoading.value) return false;
    return loadItems(reportId, selectionGeneration, true);
  }

  async function create(input: CreateReportActionItemInput): Promise<boolean> {
    const reportId = selectedReportId.value;
    if (reportId === null || mutationLoading.value) return false;
    const normalized: CreateReportActionItemInput = {
      idempotency_key: input.idempotency_key.trim(),
      title: input.title.trim(),
      description: input.description.trim(),
      owner_id: input.owner_id.trim(),
      due_at: input.due_at,
      actor_id: input.actor_id.trim(),
    };
    if (
      !/^[A-Za-z0-9][A-Za-z0-9._:-]{7,99}$/.test(normalized.idempotency_key) ||
      normalized.title.length === 0 ||
      normalized.description.length === 0 ||
      normalized.owner_id.length === 0 ||
      normalized.actor_id.length === 0 ||
      !Number.isFinite(Date.parse(normalized.due_at))
    ) {
      error.value = "请完整填写标题、说明、负责人、截止时间和操作者。";
      return false;
    }

    const selection = selectionGeneration;
    loadRequestGeneration += 1;
    loading.value = false;
    mutationLoading.value = true;
    error.value = null;
    message.value = null;
    try {
      const result = await createReportActionItem(reportId, normalized);
      if (result.action_item.report_id !== reportId) {
        throw new Error("创建结果与当前复盘报告不一致");
      }
      if (selection !== selectionGeneration || selectedReportId.value !== reportId) return false;
      const alreadyVisible = items.value.some((item) => item.id === result.action_item.id);
      items.value = upsertItem(items.value, result.action_item);
      if (!alreadyVisible && result.created) total.value += 1;
      total.value = Math.max(total.value, items.value.length);
      message.value = result.created
        ? "改进行动项已创建并持久化。"
        : "相同创建请求已安全重放，未生成重复行动项。";
      return true;
    } catch (caught) {
      if (selection === selectionGeneration && selectedReportId.value === reportId) {
        error.value = errorMessage(caught);
      }
      return false;
    } finally {
      if (selection === selectionGeneration && selectedReportId.value === reportId) {
        mutationLoading.value = false;
      }
    }
  }

  async function complete(
    itemId: string,
    actorId: string,
    completionNote: string,
  ): Promise<boolean> {
    const reportId = selectedReportId.value;
    const item = items.value.find((candidate) => candidate.id === itemId);
    if (reportId === null || item === undefined || mutationLoading.value) return false;
    if (item.status !== "open") {
      error.value = "该改进行动项已经完成，不能再次改写。";
      return false;
    }
    const normalizedActorId = actorId.trim();
    const normalizedNote = completionNote.trim();
    if (normalizedActorId.length === 0 || normalizedNote.length === 0) {
      error.value = "请填写完成操作者和完成说明。";
      return false;
    }

    const selection = selectionGeneration;
    loadRequestGeneration += 1;
    loading.value = false;
    mutationLoading.value = true;
    error.value = null;
    message.value = null;
    try {
      const result = await completeReportActionItem(item.id, {
        expected_version: item.version,
        actor_id: normalizedActorId,
        completion_note: normalizedNote,
      });
      if (result.action_item.report_id !== reportId || result.action_item.id !== item.id) {
        throw new Error("完成结果与当前改进行动项不一致");
      }
      if (selection !== selectionGeneration || selectedReportId.value !== reportId) return false;
      items.value = upsertItem(items.value, result.action_item);
      message.value = result.completion_applied
        ? "改进行动项已完成，完成审计不可修改。"
        : "相同完成请求已安全重放，未追加重复事件。";
      return true;
    } catch (caught) {
      if (selection !== selectionGeneration || selectedReportId.value !== reportId) return false;
      const failureMessage = errorMessage(caught);
      if (isCompletionConflict(caught)) {
        await loadItems(reportId, selection, false);
      }
      if (selection === selectionGeneration && selectedReportId.value === reportId) {
        error.value = failureMessage;
      }
      return false;
    } finally {
      if (selection === selectionGeneration && selectedReportId.value === reportId) {
        mutationLoading.value = false;
      }
    }
  }

  function clear(): void {
    selectionGeneration += 1;
    loadRequestGeneration += 1;
    selectedReportId.value = null;
    items.value = [];
    total.value = 0;
    loading.value = false;
    mutationLoading.value = false;
    error.value = null;
    message.value = null;
  }

  return {
    selectedReportId,
    items,
    total,
    loading,
    mutationLoading,
    error,
    message,
    selectReport,
    refresh,
    create,
    complete,
    clear,
  };
});
