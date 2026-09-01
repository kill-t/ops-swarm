<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { RouterLink } from "vue-router";

import type {
  ApprovalActionStep,
  ApprovalDecision,
  ApprovalDecisionInput,
  PendingApproval,
} from "../types/approvals";

const props = defineProps<{
  approval: PendingApproval | null;
  loading: boolean;
  error: string | null;
}>();

const emit = defineEmits<{
  decide: [decision: ApprovalDecision, input: ApprovalDecisionInput];
}>();

const actorId = ref("engineer-001");
const reason = ref("");
const confirmed = ref(false);

const canDecide = computed(
  () =>
    props.approval !== null &&
    !props.loading &&
    actorId.value.trim().length > 0 &&
    reason.value.trim().length > 0 &&
    confirmed.value,
);

const expired = computed(
  () =>
    props.approval !== null &&
    new Date(props.approval.approval_request.expires_at).getTime() <= Date.now(),
);

watch(
  () => props.approval?.approval_request.id,
  () => {
    reason.value = "";
    confirmed.value = false;
  },
);

function formatTime(value: string): string {
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

function submit(decision: ApprovalDecision): void {
  if (!canDecide.value || props.approval === null) return;
  emit("decide", decision, {
    expected_incident_version: props.approval.incident.version,
    actor_id: actorId.value.trim(),
    reason: reason.value.trim(),
  });
}
</script>

<template>
  <main class="approval-detail-panel">
    <div v-if="approval === null" class="detail-placeholder">
      <span class="placeholder-glyph">✓</span>
      <h2>选择待审批计划</h2>
      <p>批准只授予白名单执行资格，不会在本页面直接操作环境。</p>
    </div>

    <template v-else>
      <header class="approval-hero">
        <div>
          <div class="hero-topline">
            <span class="severity" :class="`severity-${approval.incident.severity}`">
              {{ approval.incident.severity.toUpperCase() }}
            </span>
            <span class="risk-badge" :class="`risk-${approval.action_plan.risk_level}`">
              {{ approval.action_plan.risk_level.toUpperCase() }} 风险
            </span>
          </div>
          <h1>{{ approval.incident.title }}</h1>
          <p>{{ approval.incident.reference }} · {{ approval.incident.service_name }}</p>
        </div>
        <RouterLink
          class="approval-incident-link"
          :to="{ name: 'incidents', params: { incidentId: approval.incident.id } }"
        >
          查看作战室
        </RouterLink>
      </header>

      <section class="approval-warning" aria-label="本地演练安全边界">
        <strong>LOCAL FAULT LAB</strong>
        <span>
          当前没有身份认证，操作者 ID 只是审计声明。批准不会执行动作；后续仍需通过确定性白名单执行器。
        </span>
      </section>

      <section class="approval-context-panel">
        <div class="section-heading">
          <h2>审批绑定</h2>
          <span :class="{ 'expiry-overdue': expired }">
            {{ expired ? "已超过有效期" : `有效期至 ${formatTime(approval.approval_request.expires_at)}` }}
          </span>
        </div>
        <dl class="approval-facts">
          <div>
            <dt>计划版本</dt>
            <dd>v{{ approval.action_plan.version }}</dd>
          </div>
          <div>
            <dt>Incident 乐观锁</dt>
            <dd>v{{ approval.incident.version }}</dd>
          </div>
          <div>
            <dt>风险策略</dt>
            <dd>{{ approval.action_plan.risk_policy_version }}</dd>
          </div>
          <div>
            <dt>内容摘要</dt>
            <dd><code>{{ approval.approval_request.plan_content_hash }}</code></dd>
          </div>
        </dl>
        <p class="approval-request-reason">
          <strong>{{ approval.approval_request.requested_by_id }}</strong>
          请求审批：{{ approval.approval_request.request_reason }}
        </p>
      </section>

      <section class="approval-plan-panel">
        <div class="section-heading">
          <h2>结构化处置计划</h2>
          <span>{{ approval.action_plan.steps.length }} 个白名单步骤</span>
        </div>
        <p class="approval-plan-summary">{{ approval.action_plan.summary }}</p>
        <ol class="approval-step-list">
          <li v-for="step in approval.action_plan.steps" :key="step.id">
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
          </li>
        </ol>
      </section>

      <section class="approval-decision-panel">
        <div class="section-heading">
          <h2>人工决定</h2>
          <span>决定写入后不可修改</span>
        </div>
        <div class="approval-form-grid">
          <label>
            操作者 ID
            <input v-model="actorId" maxlength="100" autocomplete="off" />
          </label>
          <label>
            决定原因
            <textarea
              v-model="reason"
              maxlength="500"
              rows="3"
              placeholder="说明已核对的证据、目标和恢复判据"
            />
          </label>
        </div>
        <label class="approval-confirmation">
          <input v-model="confirmed" type="checkbox" />
          我已核对计划版本、动作参数、风险说明和恢复判据
        </label>
        <p v-if="error" class="inline-error" role="alert">{{ error }}</p>
        <div class="approval-actions">
          <button
            class="decision-button reject"
            type="button"
            :disabled="!canDecide"
            @click="submit('reject')"
          >
            {{ expired ? "同步过期状态" : "拒绝并继续调查" }}
          </button>
          <button
            v-if="!expired"
            class="decision-button approve"
            type="button"
            :disabled="!canDecide"
            @click="submit('approve')"
          >
            {{ loading ? "提交中…" : "批准计划" }}
          </button>
        </div>
      </section>
    </template>
  </main>
</template>
