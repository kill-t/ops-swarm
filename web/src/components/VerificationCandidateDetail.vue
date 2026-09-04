<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { RouterLink } from "vue-router";

import type { ApprovalActionStep } from "../types/approvals";
import type {
  VerificationCandidate,
  VerificationRun,
  VerificationWorkItem,
} from "../types/verifications";

const props = defineProps<{
  candidate: VerificationCandidate | null;
  run: VerificationRun | null;
  workItem: VerificationWorkItem | null;
  creating: boolean;
  progressLoading: boolean;
  error: string | null;
}>();

const emit = defineEmits<{
  create: [];
  refresh: [];
}>();

const confirmed = ref(false);
watch(
  () => props.candidate?.operation_execution.id,
  () => {
    confirmed.value = false;
  },
);

const progressPercent = computed(() => {
  if (props.run === null) return 0;
  return Math.min(
    100,
    Math.round(
      (props.run.successful_sample_count / props.run.rule.required_successful_samples) * 100,
    ),
  );
});

function formatTime(value: string | null): string {
  if (value === null) return "尚未记录";
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(new Date(value));
}

function actionName(step: ApprovalActionStep): string {
  if (step.action_type === "rollback_release") return "回滚发布版本";
  if (step.action_type === "rollback_configuration") return "回滚数据库连接池配置";
  return "恢复 Redis 依赖";
}

function parameterSummary(step: ApprovalActionStep): string {
  if (step.action_type === "rollback_release") {
    return `${step.parameters.current_version} → ${step.parameters.target_version}`;
  }
  if (step.action_type === "rollback_configuration") {
    return `${step.parameters.key}: ${step.parameters.current_value} → ${step.parameters.target_value}`;
  }
  return `${step.parameters.dependency}: unavailable → available`;
}
</script>

<template>
  <main class="approval-detail-panel">
    <div v-if="candidate === null" class="detail-placeholder">
      <span class="placeholder-glyph">✓</span>
      <h2>选择待验证操作</h2>
      <p>执行成功只代表动作完成，固定探针满足完整观察窗口后才代表恢复。</p>
    </div>

    <template v-else>
      <header class="approval-hero">
        <div>
          <div class="hero-topline">
            <span class="severity" :class="`severity-${candidate.incident.severity}`">
              {{ candidate.incident.severity.toUpperCase() }}
            </span>
            <span class="verification-state" :class="run?.status ?? 'ready'">
              {{ run?.status.toUpperCase() ?? "READY" }}
            </span>
          </div>
          <h1>{{ candidate.incident.title }}</h1>
          <p>{{ candidate.incident.reference }} · {{ candidate.incident.service_name }}</p>
        </div>
        <RouterLink
          class="approval-incident-link"
          :to="{ name: 'incidents', params: { incidentId: candidate.incident.id } }"
        >
          查看作战室
        </RouterLink>
      </header>

      <section class="verification-boundary" aria-label="恢复验证安全边界">
        <strong>只读自动验证</strong>
        <span>
          浏览器只能启动并观察验证。信号、阈值、目标和观测值由成功操作绑定的服务端固定探针决定。
        </span>
      </section>

      <section class="approval-context-panel">
        <div class="section-heading">
          <h2>成功操作与冻结规则</h2>
          <span>Incident 乐观锁 v{{ candidate.incident.version }}</span>
        </div>
        <dl class="approval-facts verification-facts">
          <div>
            <dt>确定性动作</dt>
            <dd>{{ actionName(candidate.action_step) }}</dd>
          </div>
          <div>
            <dt>固定参数</dt>
            <dd><code>{{ parameterSummary(candidate.action_step) }}</code></dd>
          </div>
          <div>
            <dt>执行器</dt>
            <dd>{{ candidate.operation_execution.executor_name }}</dd>
          </div>
          <div>
            <dt>执行完成</dt>
            <dd>{{ formatTime(candidate.operation_execution.completed_at) }}</dd>
          </div>
        </dl>
        <div class="verification-rule">
          <code>
            {{ candidate.action_step.verification.signal }}
            {{ candidate.action_step.verification.operator }}
            {{ candidate.action_step.verification.threshold }}
          </code>
          <span>
            {{ candidate.action_step.verification.required_successful_samples }} 个成功样本，覆盖
            {{ candidate.action_step.verification.observation_window_seconds }} 秒观察窗口
          </span>
        </div>
      </section>

      <section v-if="run === null" class="approval-decision-panel">
        <div class="section-heading">
          <h2>启动验证</h2>
          <span>启动后由独立 Worker 自动采样</span>
        </div>
        <label class="approval-confirmation verification-confirmation">
          <input v-model="confirmed" type="checkbox" />
          我确认执行成功不等于恢复成功，并将以冻结阈值和完整观察窗口为最终判据
        </label>
        <button
          class="decision-button verify"
          type="button"
          :disabled="!confirmed || creating"
          @click="emit('create')"
        >
          {{ creating ? "创建中…" : "创建恢复验证" }}
        </button>
        <p v-if="error" class="inline-error" role="alert">{{ error }}</p>
      </section>

      <template v-else>
        <section class="verification-progress-panel">
          <div class="section-heading">
            <h2>采样进度</h2>
            <button
              class="icon-button"
              type="button"
              :disabled="progressLoading"
              @click="emit('refresh')"
            >
              {{ progressLoading ? "刷新中" : "刷新进度" }}
            </button>
          </div>
          <div class="verification-progress-track" aria-label="成功样本进度">
            <span :style="{ width: `${progressPercent}%` }" />
          </div>
          <p>
            成功样本 {{ run.successful_sample_count }} /
            {{ run.rule.required_successful_samples }} · 首次成功
            {{ formatTime(run.first_success_at) }} · 最近观测 {{ formatTime(run.last_observed_at) }}
          </p>
          <dl v-if="workItem" class="verification-worker-grid">
            <div>
              <dt>调度状态</dt>
              <dd>{{ workItem.status }}</dd>
            </div>
            <div>
              <dt>下一样本</dt>
              <dd>#{{ workItem.next_sample_sequence }} · {{ formatTime(workItem.available_at) }}</dd>
            </div>
            <div>
              <dt>尝试次数</dt>
              <dd>{{ workItem.attempt_count }}</dd>
            </div>
            <div>
              <dt>Worker</dt>
              <dd>{{ workItem.locked_by ?? "等待领取" }}</dd>
            </div>
          </dl>
          <p v-if="workItem?.last_error_message" class="inline-error" role="alert">
            {{ workItem.last_error_code }}：{{ workItem.last_error_message }}
          </p>
          <p v-if="error" class="inline-error" role="alert">{{ error }}</p>
        </section>

        <section class="verification-sample-panel">
          <div class="section-heading">
            <h2>不可变样本</h2>
            <span>{{ run.samples.length }} 条</span>
          </div>
          <p v-if="run.samples.length === 0" class="empty-state">Worker 尚未取得第一个样本。</p>
          <ol v-else class="verification-sample-list">
            <li v-for="sample in run.samples" :key="sample.id">
              <span>#{{ sample.sequence }}</span>
              <strong :class="sample.status">{{ sample.status.toUpperCase() }}</strong>
              <code>
                {{ sample.observed_value ?? "ERROR" }} / &lt; {{ sample.threshold }}
              </code>
              <time>{{ formatTime(sample.observed_at) }}</time>
              <small v-if="sample.error_message">{{ sample.error_message }}</small>
            </li>
          </ol>
        </section>

        <section v-if="run.status !== 'running'" class="verification-terminal" :class="run.status">
          <strong>{{ run.status === "passed" ? "恢复验证通过" : "恢复验证失败" }}</strong>
          <span v-if="run.status === 'passed'">Incident 已进入 resolved，等待工程师正式关闭。</span>
          <span v-else>{{ run.error_message }} Incident 已返回 investigating。</span>
        </section>
      </template>
    </template>
  </main>
</template>
