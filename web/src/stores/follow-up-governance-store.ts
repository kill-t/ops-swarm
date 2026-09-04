import { defineStore } from "pinia";
import { ref } from "vue";

import { listOpenReportActionItems } from "../services/api";
import type { ReportActionItemGovernanceEntry } from "../types/report-action-items";

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "发生未知错误";
}

export const useFollowUpGovernanceStore = defineStore("follow-up-governance", () => {
  const items = ref<ReportActionItemGovernanceEntry[]>([]);
  const total = ref(0);
  const asOf = ref<string | null>(null);
  const ownerId = ref<string | null>(null);
  const overdueOnly = ref(false);
  const loading = ref(false);
  const error = ref<string | null>(null);
  let requestGeneration = 0;

  async function load(ownerFilter = "", onlyOverdue = false): Promise<boolean> {
    const generation = ++requestGeneration;
    const normalizedOwnerId = ownerFilter.trim() || null;
    loading.value = true;
    error.value = null;
    try {
      const page = await listOpenReportActionItems({
        ownerId: normalizedOwnerId ?? undefined,
        overdueOnly: onlyOverdue,
      });
      if (generation !== requestGeneration) return false;
      if (page.owner_id !== normalizedOwnerId || page.overdue_only !== onlyOverdue) {
        throw new Error("改进行动项治理响应与当前筛选条件不一致");
      }
      if (
        page.items.some(
          (entry) =>
            entry.action_item.status !== "open" ||
            (normalizedOwnerId !== null && entry.action_item.owner_id !== normalizedOwnerId) ||
            (onlyOverdue && !entry.overdue),
        )
      ) {
        throw new Error("改进行动项治理响应包含不符合条件的记录");
      }
      items.value = page.items;
      total.value = page.total;
      asOf.value = page.as_of;
      ownerId.value = page.owner_id;
      overdueOnly.value = page.overdue_only;
      return true;
    } catch (caught) {
      if (generation === requestGeneration) {
        items.value = [];
        total.value = 0;
        asOf.value = null;
        error.value = errorMessage(caught);
      }
      return false;
    } finally {
      if (generation === requestGeneration) loading.value = false;
    }
  }

  function clear(): void {
    requestGeneration += 1;
    items.value = [];
    total.value = 0;
    asOf.value = null;
    ownerId.value = null;
    overdueOnly.value = false;
    loading.value = false;
    error.value = null;
  }

  return {
    items,
    total,
    asOf,
    ownerId,
    overdueOnly,
    loading,
    error,
    load,
    clear,
  };
});
