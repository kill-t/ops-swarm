import { defineStore } from "pinia";
import { computed, ref } from "vue";

import {
  closeIncident,
  getEvidenceBoard,
  getIncident,
  getIncidentWorkroom,
  listIncidents,
  listInvestigationAttempts,
} from "../services/api";
import {
  connectIncidentEvents,
  type IncidentStreamConnection,
} from "../services/incident-stream";
import type { EvidenceBoardSnapshot } from "../types/evidence-board";
import type {
  CloseIncidentAuditInput,
  Incident,
  IncidentEvent,
  IncidentStatus,
  StreamStatus,
} from "../types/incidents";
import type { InvestigationAttempt } from "../types/investigation-attempts";
import type { IncidentWorkroomSnapshot } from "../types/workroom";

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
  const workroom = ref<IncidentWorkroomSnapshot | null>(null);
  const workroomLoading = ref(false);
  const workroomError = ref<string | null>(null);
  const investigationAttempts = ref<InvestigationAttempt[]>([]);
  const attemptsLoading = ref(false);
  const attemptsError = ref<string | null>(null);
  const evidenceBoard = ref<EvidenceBoardSnapshot | null>(null);
  const evidenceBoardLoading = ref(false);
  const evidenceBoardError = ref<string | null>(null);
  const closeLoading = ref(false);
  const closeError = ref<string | null>(null);
  const closeMessage = ref<string | null>(null);
  let streamConnection: IncidentStreamConnection | null = null;
  let selectionGeneration = 0;
  let workroomRequestGeneration = 0;

  const selectedIncidentId = computed(() => selectedIncident.value?.id ?? null);

  async function refreshEvidenceBoard(
    incidentId: string,
    runId: string,
    selection: number,
    requestGeneration: number,
  ): Promise<void> {
    evidenceBoardLoading.value = true;
    evidenceBoardError.value = null;
    try {
      const response = await getEvidenceBoard(runId);
      if (response.investigation_run_id !== runId) {
        throw new Error("Evidence Board 响应与当前运行不一致");
      }
      if (
        selection !== selectionGeneration ||
        requestGeneration !== workroomRequestGeneration ||
        selectedIncident.value?.id !== incidentId ||
        workroom.value?.investigation?.id !== runId
      ) {
        return;
      }
      evidenceBoard.value = response;
    } catch (error) {
      if (
        selection === selectionGeneration &&
        requestGeneration === workroomRequestGeneration &&
        selectedIncident.value?.id === incidentId &&
        workroom.value?.investigation?.id === runId
      ) {
        evidenceBoardError.value = errorMessage(error);
      }
    } finally {
      if (
        selection === selectionGeneration &&
        requestGeneration === workroomRequestGeneration &&
        selectedIncident.value?.id === incidentId
      ) {
        evidenceBoardLoading.value = false;
      }
    }
  }

  async function refreshAttempts(
    incidentId: string,
    runId: string,
    selection: number,
    requestGeneration: number,
  ): Promise<void> {
    attemptsLoading.value = true;
    attemptsError.value = null;
    try {
      const response = await listInvestigationAttempts(runId);
      if (response.investigation_run_id !== runId) {
        throw new Error("调查尝试响应与当前运行不一致");
      }
      if (
        selection !== selectionGeneration ||
        requestGeneration !== workroomRequestGeneration ||
        selectedIncident.value?.id !== incidentId ||
        workroom.value?.investigation?.id !== runId
      ) {
        return;
      }
      investigationAttempts.value = response.items;
    } catch (error) {
      if (
        selection === selectionGeneration &&
        requestGeneration === workroomRequestGeneration &&
        selectedIncident.value?.id === incidentId &&
        workroom.value?.investigation?.id === runId
      ) {
        attemptsError.value = errorMessage(error);
      }
    } finally {
      if (
        selection === selectionGeneration &&
        requestGeneration === workroomRequestGeneration &&
        selectedIncident.value?.id === incidentId
      ) {
        attemptsLoading.value = false;
      }
    }
  }

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

  async function refreshWorkroom(incidentId: string, selection: number): Promise<void> {
    const requestGeneration = ++workroomRequestGeneration;
    workroomLoading.value = true;
    workroomError.value = null;
    try {
      const snapshot = await getIncidentWorkroom(incidentId);
      if (
        selection !== selectionGeneration ||
        requestGeneration !== workroomRequestGeneration ||
        selectedIncident.value?.id !== incidentId
      ) {
        return;
      }
      if (snapshot.incident_version < selectedIncident.value.version) {
        return;
      }
      const previousRunId = workroom.value?.investigation?.id ?? null;
      const currentRunId = snapshot.investigation?.id ?? null;
      workroom.value = snapshot;
      if (currentRunId === null) {
        investigationAttempts.value = [];
        attemptsLoading.value = false;
        attemptsError.value = null;
        evidenceBoard.value = null;
        evidenceBoardLoading.value = false;
        evidenceBoardError.value = null;
      } else {
        if (currentRunId !== previousRunId) {
          investigationAttempts.value = [];
          evidenceBoard.value = null;
        }
        await Promise.all([
          refreshAttempts(incidentId, currentRunId, selection, requestGeneration),
          refreshEvidenceBoard(incidentId, currentRunId, selection, requestGeneration),
        ]);
      }
    } catch (error) {
      if (selection === selectionGeneration && requestGeneration === workroomRequestGeneration) {
        workroomError.value = errorMessage(error);
      }
    } finally {
      if (selection === selectionGeneration && requestGeneration === workroomRequestGeneration) {
        workroomLoading.value = false;
      }
    }
  }

  async function refreshSelectedWorkroom(): Promise<void> {
    const incidentId = selectedIncident.value?.id;
    if (incidentId !== undefined) {
      await refreshWorkroom(incidentId, selectionGeneration);
    }
  }

  function receiveEvent(event: IncidentEvent): void {
    if (event.incident_id !== selectedIncident.value?.id) {
      return;
    }
    const previousVersion = selectedIncident.value.version;
    const isNewEvent = !events.value.some((item) => item.cursor === event.cursor);
    if (isNewEvent) {
      events.value = [...events.value, event].sort((left, right) => left.cursor - right.cursor);
    }
    selectedIncident.value = applyEventSnapshot(selectedIncident.value, event);
    incidents.value = incidents.value.map((incident) =>
      incident.id === event.incident_id ? applyEventSnapshot(incident, event) : incident,
    );
    if (
      isNewEvent &&
      event.event_type === "incident.status_changed" &&
      event.incident_version >= previousVersion
    ) {
      void refreshWorkroom(event.incident_id, selectionGeneration);
    }
  }

  async function selectIncident(incidentId: string): Promise<void> {
    const generation = ++selectionGeneration;
    streamConnection?.close();
    streamConnection = null;
    events.value = [];
    detailError.value = null;
    streamError.value = null;
    workroom.value = null;
    workroomError.value = null;
    investigationAttempts.value = [];
    attemptsLoading.value = false;
    attemptsError.value = null;
    evidenceBoard.value = null;
    evidenceBoardLoading.value = false;
    evidenceBoardError.value = null;
    closeLoading.value = false;
    closeError.value = null;
    closeMessage.value = null;
    workroomRequestGeneration += 1;
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
      await refreshWorkroom(incidentId, generation);
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

  async function closeSelected(input: CloseIncidentAuditInput): Promise<boolean> {
    const incident = selectedIncident.value;
    if (incident === null || closeLoading.value) return false;
    if (incident.status !== "resolved") {
      closeError.value = "只有已经确认恢复的 Incident 可以关闭。";
      return false;
    }
    const selection = selectionGeneration;
    closeLoading.value = true;
    closeError.value = null;
    closeMessage.value = null;
    try {
      const closed = await closeIncident(incident.id, {
        expected_version: incident.version,
        actor_id: input.actor_id.trim(),
        reason: input.reason.trim(),
      });
      if (selection !== selectionGeneration || selectedIncident.value?.id !== incident.id) {
        return false;
      }
      selectedIncident.value = preferNewerSnapshot(selectedIncident.value, closed);
      incidents.value = incidents.value.map((item) =>
        item.id === closed.id ? preferNewerSnapshot(item, closed) : item,
      );
      closeMessage.value = "Incident 已正式关闭，历史审计记录保持只读。";
      await refreshWorkroom(incident.id, selection);
      return true;
    } catch (error) {
      if (selection !== selectionGeneration || selectedIncident.value?.id !== incident.id) {
        return false;
      }
      closeError.value = errorMessage(error);
      try {
        const latest = await getIncident(incident.id);
        if (selection === selectionGeneration && selectedIncident.value?.id === incident.id) {
          selectedIncident.value = preferNewerSnapshot(selectedIncident.value, latest);
          incidents.value = incidents.value.map((item) =>
            item.id === latest.id ? preferNewerSnapshot(item, latest) : item,
          );
        }
      } catch {
        // 保留原始关闭错误；读取最新状态失败不能掩盖第一次失败。
      }
      return false;
    } finally {
      if (selection === selectionGeneration) closeLoading.value = false;
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
    workroomRequestGeneration += 1;
    workroom.value = null;
    workroomLoading.value = false;
    workroomError.value = null;
    investigationAttempts.value = [];
    attemptsLoading.value = false;
    attemptsError.value = null;
    evidenceBoard.value = null;
    evidenceBoardLoading.value = false;
    evidenceBoardError.value = null;
    closeLoading.value = false;
    closeError.value = null;
    closeMessage.value = null;
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
    workroom,
    workroomLoading,
    workroomError,
    investigationAttempts,
    attemptsLoading,
    attemptsError,
    evidenceBoard,
    evidenceBoardLoading,
    evidenceBoardError,
    closeLoading,
    closeError,
    closeMessage,
    loadIncidents,
    selectIncident,
    refreshSelectedWorkroom,
    closeSelected,
    clearSelection,
  };
});
