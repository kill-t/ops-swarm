import { defineStore } from "pinia";
import { computed, ref } from "vue";

import { getIncident, listIncidents } from "../services/api";
import {
  connectIncidentEvents,
  type IncidentStreamConnection,
} from "../services/incident-stream";
import type { Incident, IncidentEvent, IncidentStatus, StreamStatus } from "../types/incidents";

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "发生未知错误";
}

function preferNewerSnapshot(current: Incident | undefined, incoming: Incident): Incident {
  if (current === undefined) {
    return incoming;
  }
  if (current.version > incoming.version) {
    return current;
  }
  if (current.version === incoming.version && current.updated_at > incoming.updated_at) {
    return current;
  }
  return incoming;
}

function applyEventSnapshot(incident: Incident, event: IncidentEvent): Incident {
  if (event.incident_version < incident.version) {
    return incident;
  }
  const nextStatus: IncidentStatus = event.to_status;
  return {
    ...incident,
    status: nextStatus,
    version: event.incident_version,
    updated_at: event.occurred_at,
    resolved_at:
      nextStatus === "resolved"
        ? event.occurred_at
        : nextStatus === "investigating"
          ? null
          : incident.resolved_at,
    closed_at: nextStatus === "closed" ? event.occurred_at : incident.closed_at,
  };
}

export const useIncidentStore = defineStore("incidents", () => {
  const incidents = ref<Incident[]>([]);
  const selectedIncident = ref<Incident | null>(null);
  const events = ref<IncidentEvent[]>([]);
  const listLoading = ref(false);
  const detailLoading = ref(false);
  const listError = ref<string | null>(null);
  const detailError = ref<string | null>(null);
  const streamStatus = ref<StreamStatus>("idle");
  const streamError = ref<string | null>(null);
  let streamConnection: IncidentStreamConnection | null = null;
  let selectionGeneration = 0;

  const selectedIncidentId = computed(() => selectedIncident.value?.id ?? null);

  async function loadIncidents(): Promise<void> {
    listLoading.value = true;
    listError.value = null;
    try {
      const currentById = new Map(incidents.value.map((incident) => [incident.id, incident]));
      incidents.value = (await listIncidents()).items.map((incident) =>
        preferNewerSnapshot(currentById.get(incident.id), incident),
      );
    } catch (error) {
      listError.value = errorMessage(error);
    } finally {
      listLoading.value = false;
    }
  }

  function receiveEvent(event: IncidentEvent): void {
    if (event.incident_id !== selectedIncident.value?.id) {
      return;
    }
    if (!events.value.some((item) => item.cursor === event.cursor)) {
      events.value = [...events.value, event].sort((left, right) => left.cursor - right.cursor);
    }
    selectedIncident.value = applyEventSnapshot(selectedIncident.value, event);
    incidents.value = incidents.value.map((incident) =>
      incident.id === event.incident_id ? applyEventSnapshot(incident, event) : incident,
    );
  }

  async function selectIncident(incidentId: string): Promise<void> {
    const generation = ++selectionGeneration;
    streamConnection?.close();
    streamConnection = null;
    events.value = [];
    detailError.value = null;
    streamError.value = null;
    streamStatus.value = "connecting";
    detailLoading.value = true;
    try {
      const incident = await getIncident(incidentId);
      if (generation !== selectionGeneration) {
        return;
      }
      selectedIncident.value = incident;
      streamConnection = connectIncidentEvents(incidentId, {
        onEvent: receiveEvent,
        onStatus: (status) => {
          if (generation === selectionGeneration) {
            streamStatus.value = status;
          }
        },
        onMalformedEvent: (message) => {
          if (generation === selectionGeneration) {
            streamError.value = message;
          }
        },
      });
    } catch (error) {
      if (generation === selectionGeneration) {
        selectedIncident.value = null;
        detailError.value = errorMessage(error);
        streamStatus.value = "closed";
      }
    } finally {
      if (generation === selectionGeneration) {
        detailLoading.value = false;
      }
    }
  }

  function clearSelection(): void {
    selectionGeneration += 1;
    streamConnection?.close();
    streamConnection = null;
    selectedIncident.value = null;
    events.value = [];
    detailError.value = null;
    streamError.value = null;
    streamStatus.value = "idle";
  }

  return {
    incidents,
    selectedIncident,
    selectedIncidentId,
    events,
    listLoading,
    detailLoading,
    listError,
    detailError,
    streamStatus,
    streamError,
    loadIncidents,
    selectIncident,
    clearSelection,
  };
});
