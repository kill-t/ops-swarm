<script setup lang="ts">
import type { IncidentEvent, StreamStatus } from "../types/incidents";

defineProps<{
  events: IncidentEvent[];
  streamStatus: StreamStatus;
  streamError: string | null;
}>();

const statusText: Record<StreamStatus, string> = {
  idle: "等待选择事故",
  connecting: "正在连接",
  open: "实时连接正常",
  reconnecting: "连接中断，自动重连中",
  closed: "连接已关闭",
};

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
</script>

<template>
  <section class="timeline-panel" aria-labelledby="timeline-title">
    <div class="timeline-heading">
      <div>
        <p class="eyebrow">LIVE EVIDENCE TRAIL</p>
        <h3 id="timeline-title">事故时间线</h3>
      </div>
      <span class="connection-state" :class="`connection-${streamStatus}`" aria-live="polite">
        <span class="connection-dot" />
        {{ statusText[streamStatus] }}
      </span>
    </div>

    <p v-if="streamError" class="inline-error" role="alert">事件解析失败：{{ streamError }}</p>
    <p v-if="events.length === 0" class="empty-state">等待已持久化的事故事件。</p>

    <ol v-else class="timeline-list">
      <li v-for="event in events" :key="event.cursor" class="timeline-item">
        <span class="timeline-marker" />
        <div class="timeline-content">
          <div class="timeline-topline">
            <strong>{{ event.reason }}</strong>
            <time :datetime="event.occurred_at">{{ formatTime(event.occurred_at) }}</time>
          </div>
          <div class="timeline-meta">
            <code>#{{ event.cursor }}</code>
            <span>{{ event.event_type }}</span>
            <span v-if="event.from_status">{{ event.from_status }} → {{ event.to_status }}</span>
            <span v-else>{{ event.to_status }}</span>
            <span>{{ event.actor_id ?? event.actor_type }}</span>
          </div>
        </div>
      </li>
    </ol>
  </section>
</template>
