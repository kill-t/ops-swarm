<script setup lang="ts">
import type {
  InvestigationAttempt,
  InvestigationAttemptStatus,
} from "../types/investigation-attempts";

defineProps<{
  attempts: InvestigationAttempt[];
  loading: boolean;
  error: string | null;
}>();

const statusLabels: Record<InvestigationAttemptStatus, string> = {
  running: "进行中",
  succeeded: "成功",
  failed: "失败",
  lease_expired: "租约过期",
};

function statusLabel(status: InvestigationAttemptStatus): string {
  return statusLabels[status];
}

function formatTime(value: string): string {
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(new Date(value));
}

function formatDuration(value: number | null): string {
  if (value === null) return "尚未结算";
  if (value < 1000) return `${value} ms`;
  return `${(value / 1000).toFixed(value % 1000 === 0 ? 0 : 1)} 秒`;
}

function formatCost(attempt: InvestigationAttempt): string {
  const cost = attempt.usage.estimated_cost_usd;
  if (cost !== null) return `$${Number(cost).toFixed(6)}`;
  if (attempt.status === "running") return "等待结算";
  if (!attempt.usage.usage_complete) return "用量不完整";
  if (attempt.pricing === null) return "未配置价格";
  return "等待结算";
}

function usageNote(attempt: InvestigationAttempt): string | null {
  if (attempt.status === "running") return "等待本次尝试结算用量";
  if (attempt.usage.usage_complete) return null;
  if (attempt.usage.model_request_count === 0) return "未取得模型用量，费用未知";
  return "供应商只返回了部分模型用量，费用未知";
}

function retryLabel(attempt: InvestigationAttempt): string | null {
  if (
    attempt.will_retry === null ||
    (attempt.status !== "failed" && attempt.status !== "lease_expired")
  ) {
    return null;
  }
  return attempt.will_retry ? "将重试" : "不再重试";
}
</script>

<template>
  <section class="attempt-panel" aria-labelledby="attempt-history-title">
    <div class="attempt-heading">
      <div>
        <p class="eyebrow">WORKER ATTEMPTS</p>
        <h3 id="attempt-history-title">调查尝试</h3>
      </div>
      <span>{{ attempts.length }} 次已持久化领取</span>
    </div>

    <p v-if="error" class="inline-error" role="alert">{{ error }}</p>
    <p v-if="loading && attempts.length === 0" class="empty-state" aria-live="polite">
      正在读取调查尝试…
    </p>
    <p v-else-if="!loading && attempts.length === 0 && error === null" class="empty-state">
      当前调查尚未被 Worker 领取。
    </p>

    <ol v-else-if="attempts.length > 0" class="attempt-list" :aria-busy="loading">
      <li v-for="attempt in attempts" :key="attempt.id" class="attempt-card">
        <header class="attempt-card-heading">
          <div>
            <span class="stage-index">
              ATTEMPT {{ String(attempt.attempt_number).padStart(2, "0") }}
            </span>
            <strong>{{ attempt.model_name ?? "尚未选择模型" }}</strong>
          </div>
          <span class="stage-status" :class="`stage-status-${attempt.status}`">
            {{ statusLabel(attempt.status) }}
          </span>
        </header>

        <p class="attempt-context">
          {{ attempt.worker_id }} · 开始 {{ formatTime(attempt.started_at) }}
          <template v-if="attempt.status === 'running'">
            · 租约到期 {{ formatTime(attempt.lease_expires_at) }}
          </template>
        </p>

        <dl class="attempt-metrics">
          <div>
            <dt>耗时</dt>
            <dd>{{ formatDuration(attempt.duration_ms) }}</dd>
          </div>
          <div>
            <dt>模型请求</dt>
            <dd>{{ attempt.usage.model_request_count }}</dd>
          </div>
          <div>
            <dt>输入 Token</dt>
            <dd>{{ attempt.usage.input_tokens }}</dd>
          </div>
          <div>
            <dt>输出 Token</dt>
            <dd>{{ attempt.usage.output_tokens }}</dd>
          </div>
          <div>
            <dt>总 Token</dt>
            <dd>{{ attempt.usage.total_tokens }}</dd>
          </div>
          <div>
            <dt>估算费用</dt>
            <dd>{{ formatCost(attempt) }}</dd>
          </div>
        </dl>

        <p v-if="usageNote(attempt)" class="attempt-caveat" role="note">
          {{ usageNote(attempt) }}
        </p>
        <p v-if="attempt.error" class="attempt-error">
          <code>{{ attempt.error.error_code }}</code>
          <span>{{ attempt.error.message }}</span>
        </p>
        <footer v-if="retryLabel(attempt)" class="attempt-retry">
          {{ retryLabel(attempt) }}
        </footer>
      </li>
    </ol>
  </section>
</template>
