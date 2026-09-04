<script setup lang="ts">
import { computed, ref, watch } from "vue";

import type { Incident } from "../types/incidents";
import type { IncidentReport } from "../types/reports";

const props = defineProps<{
  incident: Incident;
  report: IncidentReport | null;
  loading: boolean;
  error: string | null;
  message: string | null;
}>();

const emit = defineEmits<{
  generate: [actorId: string];
  refresh: [];
}>();

const actorId = ref("engineer-001");
const canGenerate = computed(
  () =>
    props.incident.status === "closed" &&
    props.report === null &&
    !props.loading &&
    actorId.value.trim().length > 0,
);

watch(
  () => props.incident.id,
  () => {
    actorId.value = "engineer-001";
  },
);

function formatTime(value: string | null): string {
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

function formatDuration(seconds: number): string {
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const remainingSeconds = seconds % 60;
  if (hours > 0) return `${hours} 小时 ${minutes} 分`;
  if (minutes > 0) return `${minutes} 分 ${remainingSeconds} 秒`;
  return `${remainingSeconds} 秒`;
}

function formatConfidence(value: number): string {
  return `${Math.round(value * 100)}%`;
}

function generate(): void {
  if (canGenerate.value) emit("generate", actorId.value.trim());
}

function printReport(): void {
  window.print();
}
</script>

<template>
  <section
    v-if="incident.status === 'closed'"
    class="incident-report-panel"
    aria-labelledby="incident-report-title"
  >
    <div class="section-heading report-screen-heading">
      <div>
        <p class="eyebrow">POST-INCIDENT REVIEW</p>
        <h3 id="incident-report-title">事故复盘报告</h3>
      </div>
      <div class="report-actions">
        <button class="icon-button" type="button" :disabled="loading" @click="emit('refresh')">
          {{ loading ? "读取中" : "同步" }}
        </button>
        <button
          v-if="report"
          class="decision-button print-report"
          type="button"
          @click="printReport"
        >
          打印 / 保存 PDF
        </button>
      </div>
    </div>

    <p v-if="message" class="report-success" role="status">{{ message }}</p>
    <p v-if="report === null && loading" class="empty-state" aria-live="polite">
      正在读取已冻结的事故复盘报告…
    </p>
    <div v-else-if="report === null && error" class="report-load-error" role="alert">
      <p>{{ error }}</p>
      <button class="icon-button" type="button" @click="emit('refresh')">重新读取</button>
    </div>
    <div v-else-if="report === null" class="report-generation-panel">
      <div class="report-boundary">
        <strong>只冻结已提交事实</strong>
        <span>
          报告由 Incident、处置链路和追加式时间线确定性生成。浏览器不能填写根因、执行结果或验证结论。
        </span>
      </div>
      <label>
        报告生成操作者
        <input v-model="actorId" type="text" maxlength="100" autocomplete="off" />
        <small>当前没有登录认证，此字段仅作为本地审计声明。</small>
      </label>
      <button
        class="decision-button generate-report"
        type="button"
        :disabled="!canGenerate"
        @click="generate"
      >
        {{ loading ? "生成中…" : "冻结事故复盘报告" }}
      </button>
    </div>

    <article v-else class="incident-report-document">
      <header class="report-document-header">
        <div>
          <p>OpsSwarm · INCIDENT REPORT</p>
          <h2>{{ report.content.incident.reference }} · {{ report.content.incident.title }}</h2>
          <span>{{ report.content.incident.service_name }}</span>
        </div>
        <div class="report-document-status">
          <strong>{{ report.content.incident.severity.toUpperCase() }}</strong>
          <span>CLOSED · v{{ report.incident_version }}</span>
        </div>
      </header>

      <dl class="report-facts">
        <div>
          <dt>首次发生</dt>
          <dd>{{ formatTime(report.content.incident.started_at) }}</dd>
        </div>
        <div>
          <dt>确认恢复</dt>
          <dd>{{ formatTime(report.content.incident.resolved_at) }}</dd>
        </div>
        <div>
          <dt>正式关闭</dt>
          <dd>{{ formatTime(report.content.incident.closed_at) }}</dd>
        </div>
        <div>
          <dt>恢复耗时</dt>
          <dd>{{ formatDuration(report.content.incident.duration_seconds) }}</dd>
        </div>
      </dl>

      <section class="report-section">
        <div class="report-section-heading">
          <span>01</span>
          <h3>根因与证据</h3>
        </div>
        <template v-if="report.content.workroom.evidence_board?.primary_hypothesis">
          <p class="report-primary-statement">
            {{ report.content.workroom.evidence_board.primary_hypothesis.statement }}
          </p>
          <dl class="report-inline-facts">
            <div>
              <dt>根因分类</dt>
              <dd>{{ report.content.workroom.evidence_board.primary_hypothesis.root_cause }}</dd>
            </div>
            <div>
              <dt>置信度</dt>
              <dd>
                {{
                  formatConfidence(
                    report.content.workroom.evidence_board.primary_hypothesis.confidence,
                  )
                }}
              </dd>
            </div>
            <div>
              <dt>证据规模</dt>
              <dd>
                {{ report.content.workroom.evidence_board.evidence_count }} 条证据 ·
                {{ report.content.workroom.evidence_board.specialist_report_count }} 份专业报告
              </dd>
            </div>
          </dl>
          <p class="report-supporting-text">
            {{ report.content.workroom.evidence_board.primary_hypothesis.challenge_summary }}
          </p>
        </template>
        <p v-else class="report-empty-fact">该快照没有结构化根因假设，不由页面补写。</p>
      </section>

      <section class="report-section">
        <div class="report-section-heading">
          <span>02</span>
          <h3>处置与审批</h3>
        </div>
        <template v-if="report.content.workroom.action_plan">
          <p class="report-primary-statement">{{ report.content.workroom.action_plan.summary }}</p>
          <dl class="report-inline-facts">
            <div>
              <dt>风险等级</dt>
              <dd>{{ report.content.workroom.action_plan.risk_level }}</dd>
            </div>
            <div>
              <dt>白名单步骤</dt>
              <dd>{{ report.content.workroom.action_plan.step_count }}</dd>
            </div>
            <div>
              <dt>审批结果</dt>
              <dd>{{ report.content.workroom.approval_request?.status ?? "没有审批记录" }}</dd>
            </div>
          </dl>
          <p v-if="report.content.workroom.approval_request" class="report-supporting-text">
            {{
              report.content.workroom.approval_request.decision_reason ??
              report.content.workroom.approval_request.request_reason
            }}
          </p>
        </template>
        <p v-else class="report-empty-fact">该快照没有处置计划，不由页面推断操作。</p>
      </section>

      <section class="report-section">
        <div class="report-section-heading">
          <span>03</span>
          <h3>执行与恢复验证</h3>
        </div>
        <dl class="report-inline-facts">
          <div>
            <dt>执行状态</dt>
            <dd>{{ report.content.workroom.operation_execution?.status ?? "没有执行记录" }}</dd>
          </div>
          <div>
            <dt>执行器</dt>
            <dd>{{ report.content.workroom.operation_execution?.executor_name ?? "—" }}</dd>
          </div>
          <div>
            <dt>验证状态</dt>
            <dd>{{ report.content.workroom.verification_run?.status ?? "没有验证记录" }}</dd>
          </div>
          <div>
            <dt>健康样本</dt>
            <dd>
              <template v-if="report.content.workroom.verification_run">
                {{ report.content.workroom.verification_run.successful_sample_count }} /
                {{ report.content.workroom.verification_run.required_successful_samples }}，共保存
                {{ report.content.workroom.verification_run.sample_count }} 个样本
              </template>
              <template v-else>—</template>
            </dd>
          </div>
        </dl>
      </section>

      <section class="report-section report-timeline-section">
        <div class="report-section-heading">
          <span>04</span>
          <h3>事故时间线</h3>
        </div>
        <ol class="report-timeline">
          <li v-for="event in report.content.timeline" :key="event.cursor">
            <time>{{ formatTime(event.occurred_at) }}</time>
            <div>
              <strong>{{ event.reason }}</strong>
              <span>
                {{ event.from_status ?? "开始" }} → {{ event.to_status }} · v{{
                  event.incident_version
                }}
                · {{ event.actor_id ?? event.actor_type }}
              </span>
            </div>
          </li>
        </ol>
      </section>

      <footer class="report-document-footer">
        <div>
          <span>生成</span>
          <strong>{{ report.content.generated_by_id }} · {{ formatTime(report.created_at) }}</strong>
        </div>
        <div>
          <span>结构版本</span>
          <strong>{{ report.schema_version }}</strong>
        </div>
        <div>
          <span>内容摘要</span>
          <code>{{ report.content_hash }}</code>
        </div>
      </footer>
    </article>
  </section>
</template>
