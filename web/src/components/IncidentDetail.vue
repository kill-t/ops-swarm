<script setup lang="ts">
import IncidentTimeline from "./IncidentTimeline.vue";
import type { Incident, IncidentEvent, StreamStatus } from "../types/incidents";

defineProps<{
  incident: Incident | null;
  events: IncidentEvent[];
  loading: boolean;
  error: string | null;
  streamStatus: StreamStatus;
  streamError: string | null;
}>();

function formatDateTime(value: string | null): string {
  if (value === null) return "—";
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
</script>

<template>
  <main class="incident-detail-panel">
    <div v-if="loading" class="detail-placeholder" aria-live="polite">正在读取事故详情…</div>
    <div v-else-if="error" class="detail-placeholder error" role="alert">{{ error }}</div>
    <div v-else-if="incident === null" class="detail-placeholder">
      <span class="placeholder-glyph">⌁</span>
      <h2>选择一个 Incident</h2>
      <p>查看当前状态、关键标识和可断线重放的事故时间线。</p>
    </div>
    <template v-else>
      <header class="incident-hero">
        <div>
          <div class="hero-topline">
            <span class="reference">{{ incident.reference }}</span>
            <span class="severity" :class="`severity-${incident.severity}`">
              {{ incident.severity.toUpperCase() }}
            </span>
          </div>
          <h1>{{ incident.title }}</h1>
          <p>{{ incident.service_name }} · {{ incident.fingerprint }}</p>
        </div>
        <span class="hero-status" :class="`status-${incident.status}`">
          {{ incident.status }}
        </span>
      </header>

      <section class="facts-grid" aria-label="事故关键事实">
        <div>
          <span>当前版本</span>
          <strong>v{{ incident.version }}</strong>
        </div>
        <div>
          <span>首次发生</span>
          <strong>{{ formatDateTime(incident.started_at) }}</strong>
        </div>
        <div>
          <span>确认恢复</span>
          <strong>{{ formatDateTime(incident.resolved_at) }}</strong>
        </div>
        <div>
          <span>正式关闭</span>
          <strong>{{ formatDateTime(incident.closed_at) }}</strong>
        </div>
      </section>

      <IncidentTimeline
        :events="events"
        :stream-status="streamStatus"
        :stream-error="streamError"
      />
    </template>
  </main>
</template>
