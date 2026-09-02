<script setup lang="ts">
import type { ExecutionCandidate } from "../types/executions";

defineProps<{
  items: ExecutionCandidate[];
  total: number;
  selectedPlanId: string | null;
  loading: boolean;
  error: string | null;
}>();

const emit = defineEmits<{
  select: [planId: string];
  refresh: [];
}>();

function formatTime(value: string | null): string {
  if (value === null) return "未知";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(value));
}
</script>

<template>
  <aside class="approval-list-panel" aria-labelledby="execution-list-title">
    <div class="panel-heading">
      <div>
        <p class="eyebrow">DETERMINISTIC EXECUTION QUEUE</p>
        <h2 id="execution-list-title">受控执行</h2>
      </div>
      <button class="icon-button" type="button" :disabled="loading" @click="emit('refresh')">
        {{ loading ? "读取中" : "刷新" }}
      </button>
    </div>

    <p class="approval-count">待执行 {{ total }} 项，仅显示已批准的当前计划</p>
    <p v-if="error" class="inline-error" role="alert">{{ error }}</p>
    <p v-else-if="!loading && items.length === 0" class="empty-state">
      当前没有可以执行的已批准计划。
    </p>

    <div class="approval-queue" aria-live="polite">
      <button
        v-for="item in items"
        :key="item.action_plan.id"
        type="button"
        class="approval-card execution-card"
        :class="{ selected: item.action_plan.id === selectedPlanId }"
        :aria-current="item.action_plan.id === selectedPlanId ? 'true' : undefined"
        @click="emit('select', item.action_plan.id)"
      >
        <span class="approval-card-topline">
          <span class="reference">{{ item.incident.reference }}</span>
          <span class="severity" :class="`severity-${item.incident.severity}`">
            {{ item.incident.severity.toUpperCase() }}
          </span>
        </span>
        <strong>{{ item.incident.title }}</strong>
        <span class="approval-card-meta">
          <span>{{ item.incident.service_name }}</span>
          <span class="risk-badge" :class="`risk-${item.action_plan.risk_level}`">
            {{ item.action_plan.risk_level.toUpperCase() }} 风险
          </span>
        </span>
        <span v-if="item.active_executions.length" class="execution-running-label">
          {{ item.active_executions.length }} 个操作运行中
        </span>
        <span v-else class="approval-expiry">
          批准于 {{ formatTime(item.approval_request.decided_at) }}
        </span>
      </button>
    </div>

    <p v-if="total > items.length" class="approval-limit-note">
      当前仅展示前 {{ items.length }} 项，请先处理队列中的计划。
    </p>
  </aside>
</template>
