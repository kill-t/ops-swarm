<script setup lang="ts">
import { onMounted, onUnmounted, watch } from "vue";
import { useRoute, useRouter } from "vue-router";

import IncidentDetail from "../components/IncidentDetail.vue";
import IncidentList from "../components/IncidentList.vue";
import { useIncidentStore } from "../stores/incident-store";

const route = useRoute();
const router = useRouter();
const store = useIncidentStore();

function routeIncidentId(): string | null {
  const value = route.params.incidentId;
  return typeof value === "string" && value.length > 0 ? value : null;
}

async function selectIncident(incidentId: string): Promise<void> {
  await router.push({ name: "incidents", params: { incidentId } });
}

onMounted(async () => {
  await store.loadIncidents();
  if (routeIncidentId() === null && store.incidents.length > 0) {
    await router.replace({
      name: "incidents",
      params: { incidentId: store.incidents[0]?.id },
    });
  }
});

watch(
  () => route.params.incidentId,
  async () => {
    const incidentId = routeIncidentId();
    if (incidentId === null) {
      store.clearSelection();
      return;
    }
    await store.selectIncident(incidentId);
  },
  { immediate: true },
);

onUnmounted(() => store.clearSelection());
</script>

<template>
  <div class="workroom-shell">
    <header class="app-header">
      <div class="brand-block">
        <div class="brand-mark">OS</div>
        <div>
          <p>OpsSwarm</p>
          <span>Incident Command Center</span>
        </div>
      </div>
      <div class="environment-badge">
        <span />
        LOCAL FAULT LAB
      </div>
    </header>

    <div class="workroom-layout">
      <IncidentList
        :incidents="store.incidents"
        :selected-incident-id="store.selectedIncidentId"
        :loading="store.listLoading"
        :error="store.listError"
        @select="selectIncident"
        @refresh="store.loadIncidents"
      />
      <IncidentDetail
        :incident="store.selectedIncident"
        :events="store.events"
        :loading="store.detailLoading"
        :error="store.detailError"
        :stream-status="store.streamStatus"
        :stream-error="store.streamError"
      />
    </div>
  </div>
</template>
