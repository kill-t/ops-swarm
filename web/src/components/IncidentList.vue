<script setup lang="ts">
import type { Incident } from "../types/incidents";

defineProps<{
  incidents: Incident[];
  selectedIncidentId: string | null;
  loading: boolean;
  error: string | null;
}>();

const emit = defineEmits<{
  select: [incidentId: string];
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
</script>

<template>
  <section class="incident-list-panel" aria-labelledby="incident-list-title">
    <div class="panel-heading">
      <div>
        <p class="eyebrow">INCIDENT QUEUE</p>
        <h2 id="incident-list-title">事故列表</h2>
      </div>
      <button class="icon-button" type="button" :disabled="loading" @click="emit('refresh')">
        {{ loading ? "刷新中" : "刷新" }}
      </button>
    </div>

    <p v-if="error" class="inline-error" role="alert">{{ error }}</p>
    <p v-else-if="!loading && incidents.length === 0" class="empty-state">当前没有事故。</p>

    <div class="incident-list" aria-live="polite">
      <button
        v-for="incident in incidents"
        :key="incident.id"
        type="button"
        class="incident-card"
        :class="{ selected: incident.id === selectedIncidentId }"
        :aria-current="incident.id === selectedIncidentId ? 'true' : undefined"
        @click="emit('select', incident.id)"
      >
        <span class="incident-card-topline">
          <span class="reference">{{ incident.reference }}</span>
          <span class="severity" :class="`severity-${incident.severity}`">
            {{ incident.severity.toUpperCase() }}
          </span>
        </span>
        <strong>{{ incident.title }}</strong>
        <span class="incident-card-meta">
          <span>{{ incident.service_name }}</span>
          <time :datetime="incident.updated_at">{{ formatTime(incident.updated_at) }}</time>
        </span>
        <span class="status-label" :class="`status-${incident.status}`">
          {{ incident.status }}
        </span>
      </button>
    </div>
  </section>
</template>
