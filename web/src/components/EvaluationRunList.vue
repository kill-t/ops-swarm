<script setup lang="ts">
import type { EvaluationRunSummary } from "../types/evaluations";

defineProps<{
  runs: EvaluationRunSummary[];
  selectedRunId: string | null;
  loading: boolean;
  error: string | null;
  hasMore: boolean;
}>();

const emit = defineEmits<{
  select: [runId: string];
  refresh: [];
  loadMore: [];
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

function formatPercent(value: number): string {
  return `${(value * 100).toFixed(0)}%`;
}
</script>

<template>
  <aside class="evaluation-list-panel" aria-labelledby="evaluation-list-title">
    <div class="panel-heading">
      <div>
        <p class="eyebrow">BENCHMARK HISTORY</p>
        <h2 id="evaluation-list-title">评测运行</h2>
      </div>
      <button class="icon-button" type="button" :disabled="loading" @click="emit('refresh')">
        {{ loading ? "读取中" : "刷新" }}
      </button>
    </div>

    <p v-if="error" class="inline-error" role="alert">{{ error }}</p>
    <p v-else-if="!loading && runs.length === 0" class="empty-state">
      尚无持久化评测。请先运行离线评测命令。
    </p>

    <div class="evaluation-run-list" aria-live="polite">
      <button
        v-for="run in runs"
        :key="run.id"
        type="button"
        class="evaluation-run-card"
        :class="{ selected: run.id === selectedRunId }"
        :aria-current="run.id === selectedRunId ? 'true' : undefined"
        @click="emit('select', run.id)"
      >
        <span class="evaluation-run-topline">
          <span class="reference">RUN-{{ String(run.sequence).padStart(4, "0") }}</span>
          <time :datetime="run.generated_at">{{ formatTime(run.generated_at) }}</time>
        </span>
        <strong>{{ run.dataset_key }}</strong>
        <span class="evaluation-run-meta">
          <span>{{ run.experiment.experiment_key }}</span>
          <span>第 {{ run.repetition_index }} 次</span>
          <span>{{ run.scenario_count }} 个场景</span>
        </span>
        <span class="evaluation-scoreline">
          <span>单 Agent {{ formatPercent(run.single_agent.top1_accuracy) }}</span>
          <span>多 Agent {{ formatPercent(run.multi_agent.top1_accuracy) }}</span>
        </span>
      </button>
    </div>

    <button
      v-if="hasMore"
      class="load-more-button"
      type="button"
      :disabled="loading"
      @click="emit('loadMore')"
    >
      {{ loading ? "加载中…" : "加载更早运行" }}
    </button>
  </aside>
</template>
