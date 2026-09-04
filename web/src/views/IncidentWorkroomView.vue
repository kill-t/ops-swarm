<script setup lang="ts">
import { onMounted, onUnmounted, watch } from "vue";
import { useRoute, useRouter } from "vue-router";

import IncidentDetail from "../components/IncidentDetail.vue";
import IncidentList from "../components/IncidentList.vue";
import { useIncidentStore } from "../stores/incident-store";
import { useReportActionItemStore } from "../stores/report-action-item-store";

const route = useRoute();
const router = useRouter();
const store = useIncidentStore();
const followUpStore = useReportActionItemStore();

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

watch(
  () => store.incidentReport?.id ?? null,
  async (reportId) => {
    if (reportId === null) {
      followUpStore.clear();
      return;
    }
    await followUpStore.selectReport(reportId);
  },
  { immediate: true },
);

onUnmounted(() => {
  store.clearSelection();
  followUpStore.clear();
});
</script>

<template>
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
      :workroom="store.workroom"
      :workroom-loading="store.workroomLoading"
      :workroom-error="store.workroomError"
      :investigation-attempts="store.investigationAttempts"
      :attempts-loading="store.attemptsLoading"
      :attempts-error="store.attemptsError"
      :evidence-board="store.evidenceBoard"
      :evidence-board-loading="store.evidenceBoardLoading"
      :evidence-board-error="store.evidenceBoardError"
      :close-loading="store.closeLoading"
      :close-error="store.closeError"
      :close-message="store.closeMessage"
      :incident-report="store.incidentReport"
      :report-loading="store.reportLoading"
      :report-error="store.reportError"
      :report-message="store.reportMessage"
      :report-action-items="followUpStore.items"
      :report-action-item-total="followUpStore.total"
      :report-action-items-loading="followUpStore.loading"
      :report-action-item-mutation-loading="followUpStore.mutationLoading"
      :report-action-items-error="followUpStore.error"
      :report-action-items-message="followUpStore.message"
      @refresh-workroom="store.refreshSelectedWorkroom"
      @close-incident="store.closeSelected"
      @generate-report="store.generateSelectedReport"
      @refresh-report="store.refreshSelectedReport"
      @refresh-report-action-items="followUpStore.refresh"
      @create-report-action-item="followUpStore.create"
      @complete-report-action-item="followUpStore.complete"
    />
  </div>
</template>
