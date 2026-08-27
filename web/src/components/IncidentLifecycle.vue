<script setup lang="ts">
import type { IncidentWorkroomSnapshot } from "../types/workroom";

defineProps<{
  snapshot: IncidentWorkroomSnapshot | null;
  loading: boolean;
  error: string | null;
}>();

const emit = defineEmits<{
  refresh: [];
}>();

function formatTime(value: string | null): string {
  if (value === null) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(new Date(value));
}

function confidence(value: number): string {
  return `${Math.round(value * 100)}%`;
}
</script>

<template>
  <section class="lifecycle-panel" aria-labelledby="lifecycle-title">
    <div class="lifecycle-heading">
      <div>
        <p class="eyebrow">RESPONSE CHAIN</p>
        <h3 id="lifecycle-title">调查与处置链路</h3>
      </div>
      <button class="icon-button" type="button" :disabled="loading" @click="emit('refresh')">
        {{ loading ? "同步中" : "同步" }}
      </button>
    </div>

    <p v-if="error" class="inline-error" role="alert">{{ error }}</p>
    <p v-if="snapshot === null && loading" class="empty-state" aria-live="polite">
      正在读取同一数据库快照…
    </p>
    <p v-else-if="snapshot === null" class="empty-state">尚未取得处置链路。</p>

    <div v-else class="lifecycle-grid" :aria-busy="loading">
      <article class="stage-card" :class="{ 'stage-empty': snapshot.investigation === null }">
        <div class="stage-heading">
          <span class="stage-index">01</span>
          <span
            v-if="snapshot.investigation"
            class="stage-status"
            :class="`stage-status-${snapshot.investigation.status}`"
          >
            {{ snapshot.investigation.status }}
          </span>
        </div>
        <h4>调查运行</h4>
        <template v-if="snapshot.investigation">
          <strong>{{ snapshot.investigation.mode }}</strong>
          <p>{{ snapshot.investigation.model_name ?? "等待 Worker 选择模型" }}</p>
          <small>更新 {{ formatTime(snapshot.investigation.updated_at) }}</small>
        </template>
        <p v-else>尚未开始调查</p>
      </article>

      <article class="stage-card" :class="{ 'stage-empty': snapshot.evidence_board === null }">
        <div class="stage-heading">
          <span class="stage-index">02</span>
          <span v-if="snapshot.evidence_board?.primary_hypothesis" class="stage-status">
            {{ snapshot.evidence_board.primary_hypothesis.status }}
          </span>
        </div>
        <h4>Evidence Board</h4>
        <template v-if="snapshot.evidence_board">
          <strong>
            {{ snapshot.evidence_board.evidence_count }} 条证据 ·
            {{ snapshot.evidence_board.specialist_report_count }} 份报告
          </strong>
          <p v-if="snapshot.evidence_board.primary_hypothesis">
            {{ snapshot.evidence_board.primary_hypothesis.statement }}
          </p>
          <p v-else>等待结构化根因假设</p>
          <small v-if="snapshot.evidence_board.primary_hypothesis">
            置信度 {{ confidence(snapshot.evidence_board.primary_hypothesis.confidence) }}
          </small>
        </template>
        <p v-else>等待调查运行</p>
      </article>

      <article class="stage-card" :class="{ 'stage-empty': snapshot.action_plan === null }">
        <div class="stage-heading">
          <span class="stage-index">03</span>
          <span
            v-if="snapshot.action_plan"
            class="stage-status"
            :class="`stage-status-${snapshot.action_plan.status}`"
          >
            {{ snapshot.action_plan.status }}
          </span>
        </div>
        <h4>处置计划</h4>
        <template v-if="snapshot.action_plan">
          <strong>v{{ snapshot.action_plan.version }} · {{ snapshot.action_plan.risk_level }}</strong>
          <p>{{ snapshot.action_plan.summary }}</p>
          <small>{{ snapshot.action_plan.step_count }} 个白名单步骤</small>
        </template>
        <p v-else>尚未生成计划</p>
      </article>

      <article class="stage-card" :class="{ 'stage-empty': snapshot.approval_request === null }">
        <div class="stage-heading">
          <span class="stage-index">04</span>
          <span
            v-if="snapshot.approval_request"
            class="stage-status"
            :class="`stage-status-${snapshot.approval_request.status}`"
          >
            {{ snapshot.approval_request.status }}
          </span>
        </div>
        <h4>人工审批</h4>
        <template v-if="snapshot.approval_request">
          <strong>{{ snapshot.approval_request.requested_by_id }}</strong>
          <p>{{ snapshot.approval_request.decision_reason ?? snapshot.approval_request.request_reason }}</p>
          <small>
            {{ snapshot.approval_request.decided_at ? "决定" : "到期" }}
            {{ formatTime(snapshot.approval_request.decided_at ?? snapshot.approval_request.expires_at) }}
          </small>
        </template>
        <p v-else>等待计划提交审批</p>
      </article>

      <article class="stage-card" :class="{ 'stage-empty': snapshot.operation_execution === null }">
        <div class="stage-heading">
          <span class="stage-index">05</span>
          <span
            v-if="snapshot.operation_execution"
            class="stage-status"
            :class="`stage-status-${snapshot.operation_execution.status}`"
          >
            {{ snapshot.operation_execution.status }}
          </span>
        </div>
        <h4>白名单执行</h4>
        <template v-if="snapshot.operation_execution">
          <strong>{{ snapshot.operation_execution.executor_name }}</strong>
          <p>{{ snapshot.operation_execution.error_message ?? "确定性操作记录已持久化" }}</p>
          <small>尝试 {{ snapshot.operation_execution.attempt_count }} 次</small>
        </template>
        <p v-else>尚未执行受控操作</p>
      </article>

      <article class="stage-card" :class="{ 'stage-empty': snapshot.verification_run === null }">
        <div class="stage-heading">
          <span class="stage-index">06</span>
          <span
            v-if="snapshot.verification_run"
            class="stage-status"
            :class="`stage-status-${snapshot.verification_run.status}`"
          >
            {{ snapshot.verification_run.status }}
          </span>
        </div>
        <h4>恢复验证</h4>
        <template v-if="snapshot.verification_run">
          <strong>
            {{ snapshot.verification_run.successful_sample_count }} /
            {{ snapshot.verification_run.required_successful_samples }} 个健康样本
          </strong>
          <p>
            {{ snapshot.verification_run.signal }} &lt; {{ snapshot.verification_run.threshold }}，
            覆盖 {{ snapshot.verification_run.observation_window_seconds }} 秒
          </p>
          <small>已保存 {{ snapshot.verification_run.sample_count }} 个样本</small>
        </template>
        <p v-else>等待成功操作</p>
      </article>
    </div>
  </section>
</template>
