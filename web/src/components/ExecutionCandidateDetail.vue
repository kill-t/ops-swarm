<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { RouterLink } from "vue-router";

import { executionIdempotencyKey } from "../stores/execution-store";
import type { ApprovalActionStep } from "../types/approvals";
import type { ExecutionCandidate, OperationExecution } from "../types/executions";

const props = defineProps<{
  candidate: ExecutionCandidate | null;
  executingStepId: string | null;
  error: string | null;
}>();

const emit = defineEmits<{
  execute: [stepId: string];
}>();

const confirmed = ref(false);

watch(
  () => props.candidate?.action_plan.id,
  () => {
    confirmed.value = false;
  },
);

const busy = computed(() => props.executingStepId !== null);

function formatTime(value: string | null): string {
  if (value === null) return "未知";
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

function activeExecution(stepId: string): OperationExecution | undefined {
  return props.candidate?.active_executions.find((item) => item.action_step_id === stepId);
}

function idempotencyKey(stepId: string): string {
  if (props.candidate === null) return "";
  return executionIdempotencyKey(props.candidate.approval_request.id, stepId);
}
</script>

<template>
  <main class="approval-detail-panel">
    <div v-if="candidate === null" class="detail-placeholder">
      <span class="placeholder-glyph">↻</span>
      <h2>选择已批准计划</h2>
      <p>执行会改变本地故障演练环境，成功后才进入恢复验证。</p>
    </div>

    <template v-else>
      <header class="approval-hero">
        <div>
          <div class="hero-topline">
            <span class="severity" :class="`severity-${candidate.incident.severity}`">
              {{ candidate.incident.severity.toUpperCase() }}
            </span>
            <span class="risk-badge" :class="`risk-${candidate.action_plan.risk_level}`">
              {{ candidate.action_plan.risk_level.toUpperCase() }} 风险
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

      <section class="execution-warning" aria-label="本地演练执行边界">
        <strong>将修改 LOCAL FAULT LAB</strong>
        <span>
          浏览器不能提交命令、URL 或动作参数。服务端会重新验证最新调查、精确批准、计划摘要和 Incident 版本。
        </span>
      </section>

      <section class="approval-context-panel">
        <div class="section-heading">
          <h2>执行授权</h2>
          <span>批准于 {{ formatTime(candidate.approval_request.decided_at) }}</span>
        </div>
        <dl class="approval-facts">
          <div>
            <dt>批准人</dt>
            <dd>{{ candidate.approval_request.decided_by_id }}</dd>
          </div>
          <div>
            <dt>计划版本</dt>
            <dd>v{{ candidate.action_plan.version }}</dd>
          </div>
          <div>
            <dt>Incident 乐观锁</dt>
            <dd>v{{ candidate.incident.version }}</dd>
          </div>
          <div>
            <dt>内容摘要</dt>
            <dd><code>{{ candidate.approval_request.plan_content_hash }}</code></dd>
          </div>
        </dl>
        <p class="approval-request-reason">
          批准原因：{{ candidate.approval_request.decision_reason }}
        </p>
      </section>

      <section class="approval-plan-panel">
        <div class="section-heading">
          <h2>确定性白名单步骤</h2>
          <span>{{ candidate.action_plan.steps.length }} 个步骤</span>
        </div>
        <p class="approval-plan-summary">{{ candidate.action_plan.summary }}</p>
        <ol class="approval-step-list execution-step-list">
          <li v-for="step in candidate.action_plan.steps" :key="step.id">
            <div class="approval-step-heading">
              <span>步骤 {{ step.step_order }}</span>
              <strong>{{ actionName(step) }}</strong>
              <span class="risk-badge" :class="`risk-${step.risk_level}`">
                {{ step.risk_level.toUpperCase() }}
              </span>
            </div>
            <code>{{ parameterSummary(step) }}</code>
            <p>{{ step.risk_rationale }}</p>
            <small>
              恢复判据：{{ step.verification.signal }} {{ step.verification.operator }}
              {{ step.verification.threshold }}，连续
              {{ step.verification.required_successful_samples }} 个样本
            </small>
            <dl class="execution-step-facts">
              <div>
                <dt>步骤 ID</dt>
                <dd><code>{{ step.id }}</code></dd>
              </div>
              <div>
                <dt>固定幂等键</dt>
                <dd><code>{{ idempotencyKey(step.id) }}</code></dd>
              </div>
            </dl>
            <div v-if="activeExecution(step.id)" class="active-execution">
              <strong>操作正在执行</strong>
              <span>执行器：{{ activeExecution(step.id)?.executor_name }}</span>
              <span>幂等键：{{ activeExecution(step.id)?.idempotency_key }}</span>
              <span>尝试次数：{{ activeExecution(step.id)?.attempt_count }}</span>
              <span>租约至：{{ formatTime(activeExecution(step.id)?.locked_until ?? null) }}</span>
            </div>
            <button
              class="decision-button execute"
              type="button"
              :disabled="!confirmed || busy || activeExecution(step.id) !== undefined"
              @click="emit('execute', step.id)"
            >
              <template v-if="activeExecution(step.id)">等待运行结果</template>
              <template v-else-if="executingStepId === step.id">执行中…</template>
              <template v-else>执行此白名单步骤</template>
            </button>
          </li>
        </ol>
      </section>

      <section class="approval-decision-panel">
        <div class="section-heading">
          <h2>执行确认</h2>
          <span>批准不代表已经执行</span>
        </div>
        <label class="approval-confirmation execution-confirmation">
          <input v-model="confirmed" type="checkbox" />
          我确认当前只操作本地故障演练环境，并已复核批准绑定、精确参数、幂等键和恢复判据
        </label>
        <p v-if="error" class="inline-error" role="alert">{{ error }}</p>
      </section>
    </template>
  </main>
</template>
