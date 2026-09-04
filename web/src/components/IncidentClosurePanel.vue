<script setup lang="ts">
import { computed, ref, watch } from "vue";

import type { CloseIncidentAuditInput, Incident } from "../types/incidents";

const props = defineProps<{
  incident: Incident;
  loading: boolean;
  error: string | null;
  message: string | null;
}>();

const emit = defineEmits<{
  close: [input: CloseIncidentAuditInput];
}>();

const actorId = ref("engineer-001");
const reason = ref("");
const confirmed = ref(false);

const canClose = computed(
  () =>
    props.incident.status === "resolved" &&
    !props.loading &&
    actorId.value.trim().length > 0 &&
    reason.value.trim().length > 0 &&
    confirmed.value,
);

watch(
  () => `${props.incident.id}:${props.incident.version}`,
  () => {
    reason.value = "";
    confirmed.value = false;
  },
);

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

function submit(): void {
  if (!canClose.value) return;
  emit("close", {
    actor_id: actorId.value.trim(),
    reason: reason.value.trim(),
  });
}
</script>

<template>
  <section
    v-if="incident.status === 'resolved' || incident.status === 'closed'"
    class="incident-closure-panel"
    aria-labelledby="incident-closure-title"
  >
    <div class="section-heading">
      <div>
        <p class="eyebrow">LIFECYCLE CONTROL</p>
        <h3 id="incident-closure-title">
          {{ incident.status === "closed" ? "事故已关闭" : "人工关闭事故" }}
        </h3>
      </div>
      <span class="hero-status" :class="`status-${incident.status}`">
        {{ incident.status }} · v{{ incident.version }}
      </span>
    </div>

    <template v-if="incident.status === 'closed'">
      <p class="closure-terminal-message">
        已于 {{ formatTime(incident.closed_at) }} 进入终态。后续同指纹告警会创建新的 Incident，历史记录不会重新打开。
      </p>
      <p v-if="message" class="closure-success" role="status">{{ message }}</p>
    </template>

    <template v-else>
      <div class="closure-boundary">
        <strong>恢复不等于关闭</strong>
        <span>
          系统已于 {{ formatTime(incident.resolved_at) }} 通过恢复验证。关闭只确认本次事故生命周期结束，不会执行环境操作。
        </span>
      </div>
      <div class="approval-form-grid closure-form-grid">
        <label>
          操作者 ID
          <input v-model="actorId" maxlength="100" autocomplete="off" />
          <small>当前没有登录认证，此字段仅作为本地审计声明。</small>
        </label>
        <label>
          关闭原因
          <textarea
            v-model="reason"
            maxlength="500"
            rows="3"
            placeholder="说明恢复验证结果、观察情况和关闭依据"
          />
        </label>
      </div>
      <label class="approval-confirmation closure-confirmation">
        <input v-model="confirmed" type="checkbox" />
        我已确认恢复验证通过、当前没有需要继续跟踪的故障信号，并理解关闭后复发会创建新事故
      </label>
      <p v-if="error" class="inline-error" role="alert">{{ error }}</p>
      <button
        class="decision-button close-incident"
        type="button"
        :disabled="!canClose"
        @click="submit"
      >
        {{ loading ? "关闭中…" : "正式关闭 Incident" }}
      </button>
    </template>
  </section>
</template>
