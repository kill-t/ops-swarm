<script setup lang="ts">
import IncidentTimeline from "./IncidentTimeline.vue";
import IncidentClosurePanel from "./IncidentClosurePanel.vue";
import IncidentLifecycle from "./IncidentLifecycle.vue";
import IncidentReportPanel from "./IncidentReportPanel.vue";
import InvestigationAttemptHistory from "./InvestigationAttemptHistory.vue";
import DeploymentProvenance from "./DeploymentProvenance.vue";
import ReportActionItemsPanel from "./ReportActionItemsPanel.vue";
import type { EvidenceBoardSnapshot } from "../types/evidence-board";
import type {
  CloseIncidentAuditInput,
  Incident,
  IncidentEvent,
  StreamStatus,
} from "../types/incidents";
import type { InvestigationAttempt } from "../types/investigation-attempts";
import type { IncidentReport } from "../types/reports";
import type {
  CreateReportActionItemInput,
  ReportActionItem,
} from "../types/report-action-items";
import type { IncidentWorkroomSnapshot } from "../types/workroom";

defineProps<{
  incident: Incident | null;
  events: IncidentEvent[];
  loading: boolean;
  error: string | null;
  streamStatus: StreamStatus;
  streamError: string | null;
  workroom: IncidentWorkroomSnapshot | null;
  workroomLoading: boolean;
  workroomError: string | null;
  investigationAttempts: InvestigationAttempt[];
  attemptsLoading: boolean;
  attemptsError: string | null;
  evidenceBoard: EvidenceBoardSnapshot | null;
  evidenceBoardLoading: boolean;
  evidenceBoardError: string | null;
  closeLoading: boolean;
  closeError: string | null;
  closeMessage: string | null;
  incidentReport: IncidentReport | null;
  reportLoading: boolean;
  reportError: string | null;
  reportMessage: string | null;
  reportActionItems: ReportActionItem[];
  reportActionItemTotal: number;
  reportActionItemsLoading: boolean;
  reportActionItemMutationLoading: boolean;
  reportActionItemsError: string | null;
  reportActionItemsMessage: string | null;
}>();

const emit = defineEmits<{
  refreshWorkroom: [];
  closeIncident: [input: CloseIncidentAuditInput];
  generateReport: [actorId: string];
  refreshReport: [];
  refreshReportActionItems: [];
  createReportActionItem: [input: CreateReportActionItemInput];
  completeReportActionItem: [itemId: string, actorId: string, completionNote: string];
}>();

function forwardReportActionItemCompletion(
  itemId: string,
  actorId: string,
  completionNote: string,
): void {
  emit("completeReportActionItem", itemId, actorId, completionNote);
}

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

      <IncidentLifecycle
        :snapshot="workroom"
        :loading="workroomLoading"
        :error="workroomError"
        @refresh="emit('refreshWorkroom')"
      />

      <DeploymentProvenance
        v-if="workroom?.investigation"
        :board="evidenceBoard"
        :loading="evidenceBoardLoading"
        :error="evidenceBoardError"
      />

      <InvestigationAttemptHistory
        v-if="workroom?.investigation"
        :attempts="investigationAttempts"
        :loading="attemptsLoading"
        :error="attemptsError"
      />

      <IncidentClosurePanel
        :incident="incident"
        :loading="closeLoading"
        :error="closeError"
        :message="closeMessage"
        @close="emit('closeIncident', $event)"
      />

      <IncidentReportPanel
        :incident="incident"
        :report="incidentReport"
        :loading="reportLoading"
        :error="reportError"
        :message="reportMessage"
        @generate="emit('generateReport', $event)"
        @refresh="emit('refreshReport')"
      />

      <ReportActionItemsPanel
        v-if="incidentReport"
        :report-id="incidentReport.id"
        :items="reportActionItems"
        :total="reportActionItemTotal"
        :loading="reportActionItemsLoading"
        :mutation-loading="reportActionItemMutationLoading"
        :error="reportActionItemsError"
        :message="reportActionItemsMessage"
        @refresh="emit('refreshReportActionItems')"
        @create="emit('createReportActionItem', $event)"
        @complete="forwardReportActionItemCompletion"
      />

      <IncidentTimeline
        :events="events"
        :stream-status="streamStatus"
        :stream-error="streamError"
      />
    </template>
  </main>
</template>
