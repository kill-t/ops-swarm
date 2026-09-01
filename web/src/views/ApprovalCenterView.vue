<script setup lang="ts">
import { computed, onMounted, onUnmounted, watch } from "vue";
import { useRoute, useRouter } from "vue-router";

import ApprovalQueueList from "../components/ApprovalQueueList.vue";
import ApprovalRequestDetail from "../components/ApprovalRequestDetail.vue";
import { useApprovalStore } from "../stores/approval-store";
import type { ApprovalDecision, ApprovalDecisionInput } from "../types/approvals";

const route = useRoute();
const router = useRouter();
const store = useApprovalStore();

const routeRequestId = computed(() => {
  const value = route.params.requestId;
  return typeof value === "string" && value.length > 0 ? value : null;
});

async function selectRequest(requestId: string): Promise<void> {
  await router.push({ name: "approvals", params: { requestId } });
}

async function normalizeSelection(): Promise<void> {
  const routeId = routeRequestId.value;
  const routeExists = store.items.some((item) => item.approval_request.id === routeId);
  const requestId = routeExists ? routeId : (store.items[0]?.approval_request.id ?? null);
  if (requestId === null) {
    store.selectApproval(null);
    if (routeId !== null) await router.replace({ name: "approvals" });
    return;
  }
  store.selectApproval(requestId);
  if (requestId !== routeId) {
    await router.replace({ name: "approvals", params: { requestId } });
  }
}

async function refresh(): Promise<void> {
  await store.loadPending();
  await normalizeSelection();
}

async function decide(
  decision: ApprovalDecision,
  input: ApprovalDecisionInput,
): Promise<void> {
  await store.decide(decision, input);
  await normalizeSelection();
}

onMounted(refresh);

watch(routeRequestId, (requestId) => store.selectApproval(requestId), { immediate: true });

onUnmounted(() => store.clearSelection());
</script>

<template>
  <div>
    <p v-if="store.actionMessage" class="approval-global-message" role="status">
      {{ store.actionMessage }}
    </p>
    <p
      v-if="store.actionError && store.selectedApproval === null"
      class="approval-global-error"
      role="alert"
    >
      {{ store.actionError }}
    </p>
    <div class="approval-layout">
      <ApprovalQueueList
        :items="store.items"
        :total="store.total"
        :selected-request-id="store.selectedRequestId"
        :loading="store.listLoading"
        :error="store.listError"
        @select="selectRequest"
        @refresh="refresh"
      />
      <ApprovalRequestDetail
        :approval="store.selectedApproval"
        :loading="store.actionLoading"
        :error="store.actionError"
        @decide="decide"
      />
    </div>
  </div>
</template>
