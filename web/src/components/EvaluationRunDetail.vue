<script setup lang="ts">
import { computed } from "vue";

import type {
  EvaluationRun,
  InvestigationMode,
  InvestigationModeBenchmark,
  Recommendation,
  RootCause,
} from "../types/evaluations";

const props = defineProps<{
  run: EvaluationRun | null;
  loading: boolean;
  error: string | null;
}>();

interface MetricRow {
  label: string;
  single: string;
  multi: string;
  delta: string;
  hint: string;
}

const rootCauseLabels: Record<RootCause, string> = {
  release_500_regression: "新版本回归",
  database_pool_misconfiguration: "数据库连接池配置错误",
  redis_unavailable: "Redis 不可用",
  undetermined: "根因未确定",
};

const recommendationLabels: Record<Recommendation, string> = {
  rollback_release: "回滚发布版本",
  continue_investigation: "继续调查",
};

const modeLabels: Record<InvestigationMode, string> = {
  single_agent: "单 Agent",
  multi_agent: "多 Agent",
};

function percent(value: number): string {
  return `${(value * 100).toFixed(1)}%`;
}

function percentagePoint(value: number): string {
  const points = value * 100;
  return `${points > 0 ? "+" : ""}${points.toFixed(1)} pp`;
}

function signed(value: number, suffix: string): string {
  return `${value > 0 ? "+" : ""}${value.toFixed(3)}${suffix}`;
}

function formatDateTime(value: string): string {
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

function modeLabel(mode: InvestigationMode): string {
  return modeLabels[mode];
}

function rootCauseLabel(rootCause: RootCause): string {
  return rootCauseLabels[rootCause];
}

function recommendationLabel(recommendation: Recommendation): string {
  return recommendationLabels[recommendation];
}

function modeEvidence(mode: InvestigationModeBenchmark): string {
  return mode.referenced_evidence_ids.length === 0
    ? "未引用证据"
    : mode.referenced_evidence_ids.join(" · ");
}

const metricRows = computed<MetricRow[]>(() => {
  const run = props.run;
  if (run === null) return [];
  return [
    {
      label: "根因 Top-1",
      single: percent(run.single_agent.top1_accuracy),
      multi: percent(run.multi_agent.top1_accuracy),
      delta: percentagePoint(run.delta.top1_accuracy),
      hint: "越高越好",
    },
    {
      label: "证据召回",
      single: percent(run.single_agent.evidence_recall),
      multi: percent(run.multi_agent.evidence_recall),
      delta: percentagePoint(run.delta.evidence_recall),
      hint: "越高越好",
    },
    {
      label: "建议准确率",
      single: percent(run.single_agent.recommendation_accuracy),
      multi: percent(run.multi_agent.recommendation_accuracy),
      delta: percentagePoint(run.delta.recommendation_accuracy),
      hint: "越高越好",
    },
    {
      label: "安全违规率",
      single: percent(run.single_agent.safety_violation_rate),
      multi: percent(run.multi_agent.safety_violation_rate),
      delta: percentagePoint(run.delta.safety_violation_rate),
      hint: "越低越好",
    },
    {
      label: "平均工具调用",
      single: run.single_agent.mean_tool_call_count.toFixed(2),
      multi: run.multi_agent.mean_tool_call_count.toFixed(2),
      delta: signed(run.delta.mean_tool_call_count, ""),
      hint: "需结合质量判断",
    },
    {
      label: "平均耗时",
      single: `${run.single_agent.mean_duration_ms.toFixed(3)} ms`,
      multi: `${run.multi_agent.mean_duration_ms.toFixed(3)} ms`,
      delta: signed(run.delta.mean_duration_ms, " ms"),
      hint: "单次本地结果",
    },
  ];
});
</script>

<template>
  <main class="evaluation-detail-panel">
    <div v-if="loading" class="detail-placeholder" aria-live="polite">正在读取评测详情…</div>
    <div v-else-if="error" class="detail-placeholder error" role="alert">{{ error }}</div>
    <div v-else-if="run === null" class="detail-placeholder">
      <span class="placeholder-glyph">≋</span>
      <h2>选择一次评测运行</h2>
      <p>查看单 Agent 与多 Agent 在同一冻结数据集上的公平对照。</p>
    </div>
    <template v-else>
      <header class="evaluation-hero">
        <div>
          <div class="hero-topline">
            <span class="reference">RUN-{{ String(run.sequence).padStart(4, "0") }}</span>
            <span class="evaluation-version">{{ run.scoring_policy_version }}</span>
          </div>
          <h1>{{ run.dataset_key }}</h1>
          <p>{{ run.runner_name }} · {{ run.scenario_count }} 个冻结场景</p>
        </div>
        <time :datetime="run.generated_at">{{ formatDateTime(run.generated_at) }}</time>
      </header>

      <section class="evaluation-integrity" aria-label="评测完整性信息">
        <div>
          <span>数据集 SHA-256</span>
          <code :title="run.dataset_content_hash">{{ run.dataset_content_hash }}</code>
        </div>
        <div>
          <span>报告 SHA-256</span>
          <code :title="run.report_content_hash">{{ run.report_content_hash }}</code>
        </div>
      </section>

      <aside class="evaluation-caveat" role="note">
        <strong>结果解释边界</strong>
        <span>
          当前是冻结假模型契约评测。满分只证明数据、LangChain、LangGraph、证据护栏和评分链路可复现，不证明多 Agent 在真实模型上更强。
        </span>
      </aside>

      <section class="metric-panel" aria-labelledby="metric-comparison-title">
        <div class="section-heading">
          <div>
            <p class="eyebrow">AGGREGATE COMPARISON</p>
            <h2 id="metric-comparison-title">聚合指标对照</h2>
          </div>
          <span>差值 = 多 Agent − 单 Agent</span>
        </div>
        <div class="metric-table" role="table" aria-label="单 Agent 与多 Agent 聚合指标">
          <div class="metric-row metric-header" role="row">
            <span role="columnheader">指标</span>
            <span role="columnheader">单 Agent</span>
            <span role="columnheader">多 Agent</span>
            <span role="columnheader">差值</span>
          </div>
          <div v-for="metric in metricRows" :key="metric.label" class="metric-row" role="row">
            <span role="cell">
              <strong>{{ metric.label }}</strong>
              <small>{{ metric.hint }}</small>
            </span>
            <code role="cell">{{ metric.single }}</code>
            <code role="cell">{{ metric.multi }}</code>
            <code role="cell">{{ metric.delta }}</code>
          </div>
        </div>
      </section>

      <section class="scenario-panel" aria-labelledby="scenario-comparison-title">
        <div class="section-heading">
          <div>
            <p class="eyebrow">SCENARIO EVIDENCE</p>
            <h2 id="scenario-comparison-title">逐场景结果</h2>
          </div>
          <span>{{ run.report.scenario_results.length }} 个可复核样本</span>
        </div>

        <article
          v-for="(scenario, index) in run.report.scenario_results"
          :key="scenario.scenario_id"
          class="scenario-card"
        >
          <header class="scenario-heading">
            <div>
              <span class="stage-index">SCENARIO {{ String(index + 1).padStart(2, "0") }}</span>
              <h3>{{ scenario.scenario_id }}</h3>
            </div>
            <span>{{ scenario.execution_order.map(modeLabel).join(" → ") }}</span>
          </header>
          <div class="scenario-truth">
            <span>冻结真值：{{ rootCauseLabel(scenario.expected_root_cause) }}</span>
            <span>期望建议：{{ recommendationLabel(scenario.expected_recommendation) }}</span>
          </div>
          <div class="mode-comparison-grid">
            <section
              v-for="mode in [scenario.single_agent, scenario.multi_agent]"
              :key="mode.mode"
              class="mode-result-card"
              :aria-label="`${modeLabel(mode.mode)} 场景结果`"
            >
              <div class="mode-result-heading">
                <strong>{{ modeLabel(mode.mode) }}</strong>
                <span>{{ mode.duration_ms.toFixed(3) }} ms</span>
              </div>
              <dl>
                <div>
                  <dt>根因</dt>
                  <dd>{{ rootCauseLabel(mode.root_cause) }}</dd>
                </div>
                <div>
                  <dt>建议</dt>
                  <dd>{{ recommendationLabel(mode.recommendation) }}</dd>
                </div>
                <div>
                  <dt>证据召回</dt>
                  <dd>{{ percent(mode.metrics.evidence_recall) }}</dd>
                </div>
                <div>
                  <dt>工具调用</dt>
                  <dd>{{ mode.metrics.tool_call_count }}</dd>
                </div>
              </dl>
              <p class="mode-evidence" :title="modeEvidence(mode)">{{ modeEvidence(mode) }}</p>
            </section>
          </div>
        </article>
      </section>
    </template>
  </main>
</template>
