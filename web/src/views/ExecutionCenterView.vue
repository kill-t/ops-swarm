<script setup lang="ts">
import { computed, onMounted, onUnmounted, watch } from "vue";
import { useRoute, useRouter } from "vue-router";

import ExecutionCandidateDetail from "../components/ExecutionCandidateDetail.vue";
import ExecutionCandidateList from "../components/ExecutionCandidateList.vue";
import { useExecutionStore } from "../stores/execution-store";

const route = useRoute();
const router = useRouter();
const store = useExecutionStore();

const routePlanId = computed(() => {
  const value = route.params.planId;
  return typeof value === "string" && value.length > 0 ? value : null;
});

async function selectPlan(planId: string): Promise<void> {
  store.actionError = null;
  await router.push({ name: "executions", params: { planId } });
}

async function normalizeSelection(): Promise<void> {
  const routeId = routePlanId.value;
  const routeExists = store.items.some((item) => item.action_plan.id === routeId);
  const planId = routeExists ? routeId : (store.items[0]?.action_plan.id ?? null);
  if (planId === null) {
    store.selectCandidate(null);
    if (routeId !== null) await router.replace({ name: "executions" });
    return;
  }
  store.selectCandidate(planId);
  if (planId !== routeId) {
    await router.replace({ name: "executions", params: { planId } });
  }
}

async function refresh(): Promise<void> {
  await store.loadCandidates();
  await normalizeSelection();
}

async function execute(stepId: string): Promise<void> {
  await store.execute(stepId);
  await normalizeSelection();
}

onMounted(refresh);
watch(routePlanId, (planId) => store.selectCandidate(planId), { immediate: true });
onUnmounted(() => store.clearSelection());
</script>

<template>
  <div>
    <p v-if="store.actionMessage" class="approval-global-message" role="status">
      {{ store.actionMessage }}
    </p>
    <p
      v-if="store.actionError && store.selectedCandidate === null"
      class="approval-global-error"
      role="alert"
    >
      {{ store.actionError }}
    </p>
    <section v-if="store.lastOutcome" class="execution-outcome" aria-label="最近执行结果">
      <div>
        <strong>{{ store.lastOutcome.operation_execution.status.toUpperCase() }}</strong>
        <span>{{ store.lastOutcome.operation_execution.executor_name }}</span>
        <span>尝试 {{ store.lastOutcome.operation_execution.attempt_count }} 次</span>
      </div>
      <code>{{ store.lastOutcome.operation_execution.idempotency_key }}</code>
      <pre v-if="store.lastOutcome.operation_execution.result">{{
        JSON.stringify(store.lastOutcome.operation_execution.result, null, 2)
      }}</pre>
    </section>
    <div class="approval-layout">
      <ExecutionCandidateList
        :items="store.items"
        :total="store.total"
        :selected-plan-id="store.selectedPlanId"
        :loading="store.listLoading"
        :error="store.listError"
        @select="selectPlan"
        @refresh="refresh"
      />
      <ExecutionCandidateDetail
        :candidate="store.selectedCandidate"
        :executing-step-id="store.executingStepId"
        :error="store.actionError"
        @execute="execute"
      />
    </div>
  </div>
</template>
