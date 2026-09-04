<script setup lang="ts">
import { computed, onMounted, onUnmounted, watch } from "vue";
import { useRoute, useRouter } from "vue-router";

import VerificationCandidateDetail from "../components/VerificationCandidateDetail.vue";
import VerificationCandidateList from "../components/VerificationCandidateList.vue";
import { useVerificationStore } from "../stores/verification-store";

const route = useRoute();
const router = useRouter();
const store = useVerificationStore();
let pollingTimer: number | null = null;

const routeExecutionId = computed(() => {
  const value = route.params.executionId;
  return typeof value === "string" && value.length > 0 ? value : null;
});

const displayCandidate = computed(
  () => store.selectedCandidate ?? store.observedCandidate,
);

async function selectExecution(executionId: string): Promise<void> {
  store.actionError = null;
  await router.push({ name: "verifications", params: { executionId } });
}

async function normalizeSelection(): Promise<void> {
  const routeId = routeExecutionId.value;
  const routeExists = store.items.some(
    (item) => item.operation_execution.id === routeId,
  );
  const executionId = routeExists
    ? routeId
    : (store.items[0]?.operation_execution.id ?? null);
  if (executionId === null) {
    store.selectCandidate(null);
    if (routeId !== null) await router.replace({ name: "verifications" });
    return;
  }
  store.selectCandidate(executionId);
  if (executionId !== routeId) {
    await router.replace({ name: "verifications", params: { executionId } });
  }
}

async function refresh(): Promise<void> {
  await store.loadCandidates();
  await normalizeSelection();
}

async function createRun(): Promise<void> {
  await store.createRun();
  await normalizeSelection();
}

onMounted(async () => {
  await refresh();
  pollingTimer = window.setInterval(() => {
    if (store.observedRun?.status === "running" && !store.progressLoading) {
      void store.refreshProgress();
    }
  }, 3_000);
});
watch(routeExecutionId, (executionId) => store.selectCandidate(executionId), {
  immediate: true,
});
onUnmounted(() => {
  if (pollingTimer !== null) window.clearInterval(pollingTimer);
  store.clearSelection();
});
</script>

<template>
  <div>
    <p v-if="store.actionMessage" class="approval-global-message" role="status">
      {{ store.actionMessage }}
    </p>
    <p
      v-if="store.actionError && displayCandidate === null"
      class="approval-global-error"
      role="alert"
    >
      {{ store.actionError }}
    </p>
    <div class="approval-layout">
      <VerificationCandidateList
        :items="store.items"
        :total="store.total"
        :selected-execution-id="store.selectedExecutionId"
        :loading="store.listLoading"
        :error="store.listError"
        @select="selectExecution"
        @refresh="refresh"
      />
      <VerificationCandidateDetail
        :candidate="displayCandidate"
        :run="store.observedRun"
        :work-item="store.observedWorkItem"
        :creating="store.creating"
        :progress-loading="store.progressLoading"
        :error="store.actionError"
        @create="createRun"
        @refresh="store.refreshProgress"
      />
    </div>
  </div>
</template>
