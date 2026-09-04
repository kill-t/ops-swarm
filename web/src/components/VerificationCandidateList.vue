<script setup lang="ts">
import type { VerificationCandidate } from "../types/verifications";

defineProps<{
  items: VerificationCandidate[];
  total: number;
  selectedExecutionId: string | null;
  loading: boolean;
  error: string | null;
}>();

const emit = defineEmits<{
  select: [executionId: string];
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
  <aside class="approval-list-panel" aria-labelledby="verification-list-title">
    <div class="panel-heading">
      <div>
        <p class="eyebrow">RECOVERY VERIFICATION QUEUE</p>
        <h2 id="verification-list-title">恢复验证</h2>
      </div>
      <button class="icon-button" type="button" :disabled="loading" @click="emit('refresh')">
        {{ loading ? "读取中" : "刷新" }}
      </button>
    </div>

    <p class="approval-count">待验证 {{ total }} 项，仅显示当前计划的成功操作</p>
    <p v-if="error" class="inline-error" role="alert">{{ error }}</p>
    <p v-else-if="!loading && items.length === 0" class="empty-state">
      当前没有等待恢复验证的操作。
    </p>

    <div class="approval-queue" aria-live="polite">
      <button
        v-for="item in items"
        :key="item.operation_execution.id"
        type="button"
        class="approval-card verification-card"
        :class="{ selected: item.operation_execution.id === selectedExecutionId }"
        :aria-current="item.operation_execution.id === selectedExecutionId ? 'true' : undefined"
        @click="emit('select', item.operation_execution.id)"
      >
        <span class="approval-card-topline">
          <span class="reference">{{ item.incident.reference }}</span>
          <span class="severity" :class="`severity-${item.incident.severity}`">
            {{ item.incident.severity.toUpperCase() }}
          </span>
        </span>
        <strong>{{ item.incident.title }}</strong>
        <span class="approval-card-meta">
          <span>{{ item.action_step.verification.signal }}</span>
          <span class="verification-state" :class="item.verification_run?.status ?? 'ready'">
            {{ item.verification_run === null ? "待启动" : "采样中" }}
          </span>
        </span>
        <span v-if="item.verification_run" class="approval-expiry">
          已成功 {{ item.verification_run.successful_sample_count }} /
          {{ item.verification_run.rule.required_successful_samples }} 个样本
        </span>
        <span v-else class="approval-expiry">
          执行完成于 {{ formatTime(item.operation_execution.completed_at) }}
        </span>
      </button>
    </div>

    <p v-if="total > items.length" class="approval-limit-note">
      当前仅展示前 {{ items.length }} 项，请先处理队列中的操作。
    </p>
  </aside>
</template>
