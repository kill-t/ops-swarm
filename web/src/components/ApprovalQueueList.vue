<script setup lang="ts">
import type { PendingApproval } from "../types/approvals";

defineProps<{
  items: PendingApproval[];
  total: number;
  selectedRequestId: string | null;
  loading: boolean;
  error: string | null;
}>();

const emit = defineEmits<{
  select: [requestId: string];
  refresh: [];
}>();

function formatTime(value: string): string {
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(value));
}

function isExpired(value: string): boolean {
  return new Date(value).getTime() <= Date.now();
}
</script>

<template>
  <aside class="approval-list-panel" aria-labelledby="approval-list-title">
    <div class="panel-heading">
      <div>
        <p class="eyebrow">HUMAN APPROVAL QUEUE</p>
        <h2 id="approval-list-title">审批中心</h2>
      </div>
      <button class="icon-button" type="button" :disabled="loading" @click="emit('refresh')">
        {{ loading ? "读取中" : "刷新" }}
      </button>
    </div>

    <p class="approval-count">待处理 {{ total }} 项，按最早过期时间排序</p>
    <p v-if="error" class="inline-error" role="alert">{{ error }}</p>
    <p v-else-if="!loading && items.length === 0" class="empty-state">
      当前没有等待人工决定的计划。
    </p>

    <div class="approval-queue" aria-live="polite">
      <button
        v-for="item in items"
        :key="item.approval_request.id"
        type="button"
        class="approval-card"
        :class="{ selected: item.approval_request.id === selectedRequestId }"
        :aria-current="item.approval_request.id === selectedRequestId ? 'true' : undefined"
        @click="emit('select', item.approval_request.id)"
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
        <span
          class="approval-expiry"
          :class="{ expired: isExpired(item.approval_request.expires_at) }"
        >
          {{ isExpired(item.approval_request.expires_at) ? "已超过有效期" : "有效期至" }}
          {{ formatTime(item.approval_request.expires_at) }}
        </span>
      </button>
    </div>

    <p v-if="total > items.length" class="approval-limit-note">
      当前仅展示前 {{ items.length }} 项，请先处理最早到期请求。
    </p>
  </aside>
</template>
