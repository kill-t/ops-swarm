<script setup lang="ts">
import { computed, onMounted, onUnmounted, watch } from "vue";
import { useRoute, useRouter } from "vue-router";

import EvaluationRunDetail from "../components/EvaluationRunDetail.vue";
import EvaluationRunList from "../components/EvaluationRunList.vue";
import { useEvaluationStore } from "../stores/evaluation-store";

const route = useRoute();
const router = useRouter();
const store = useEvaluationStore();

const routeRunId = computed(() => {
  const value = route.params.runId;
  return typeof value === "string" && value.length > 0 ? value : null;
});

async function selectRun(runId: string): Promise<void> {
  await router.push({ name: "evaluations", params: { runId } });
}

onMounted(async () => {
  await store.loadRuns();
  if (routeRunId.value === null && store.runs.length > 0) {
    await router.replace({ name: "evaluations", params: { runId: store.runs[0]?.id } });
  }
});

watch(
  routeRunId,
  async (runId) => {
    if (runId === null) {
      store.clearSelection();
      return;
    }
    await store.selectRun(runId);
  },
  { immediate: true },
);

onUnmounted(() => store.clearSelection());
</script>

<template>
  <div class="evaluation-layout">
    <EvaluationRunList
      :runs="store.runs"
      :selected-run-id="routeRunId"
      :loading="store.listLoading"
      :error="store.listError"
      :has-more="store.nextBeforeSequence !== null"
      @select="selectRun"
      @refresh="store.loadRuns"
      @load-more="store.loadRuns(false)"
    />
    <EvaluationRunDetail
      :run="store.selectedRun"
      :loading="store.detailLoading"
      :error="store.detailError"
    />
  </div>
</template>
