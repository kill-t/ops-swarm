<script setup lang="ts">
import { computed } from "vue";
import { RouterLink } from "vue-router";

import type { ReportActionItemGovernanceEntry } from "../types/report-action-items";

const props = defineProps<{
  items: ReportActionItemGovernanceEntry[];
  total: number;
  asOf: string | null;
  loading: boolean;
  error: string | null;
}>();

const overdueCount = computed(() => props.items.filter((entry) => entry.overdue).length);

defineEmits<{
  refresh: [];
}>();

function formatTime(value: string): string {
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(value));
}
</script>

<template>
  <section class="governance-results" aria-labelledby="governance-results-title">
    <div class="section-heading">
      <div>
        <p class="eyebrow">OPEN FOLLOW-UP WORK</p>
        <h2 id="governance-results-title">未完成改进工作</h2>
      </div>
      <button class="icon-button" type="button" :disabled="loading" @click="$emit('refresh')">
        {{ loading ? "读取中" : "刷新" }}
      </button>
    </div>

    <div class="governance-summary">
      <div>
        <span>筛选结果</span>
        <strong>{{ total }}</strong>
      </div>
      <div>
        <span>当前页逾期</span>
        <strong>{{ overdueCount }}</strong>
      </div>
      <div>
        <span>服务器观察时间</span>
        <strong>{{ asOf ? formatTime(asOf) : "—" }}</strong>
      </div>
    </div>

    <p v-if="error" class="governance-error" role="alert">{{ error }}</p>
    <p v-else-if="loading && items.length === 0" class="empty-state" aria-live="polite">
      正在读取跨事故改进行动项…
    </p>
    <p v-else-if="items.length === 0" class="empty-state">
      当前筛选条件下没有未完成行动项。
    </p>

    <ol v-else class="governance-action-list">
      <li
        v-for="entry in items"
        :key="entry.action_item.id"
        class="governance-action-card"
        :class="{ overdue: entry.overdue }"
      >
        <div class="governance-action-topline">
          <div>
            <span class="reference">{{ entry.incident.reference }}</span>
            <span class="severity" :class="`severity-${entry.incident.severity}`">
              {{ entry.incident.severity.toUpperCase() }}
            </span>
            <span v-if="entry.overdue" class="governance-overdue">已逾期</span>
          </div>
          <RouterLink
            :to="{ name: 'incidents', params: { incidentId: entry.incident.id } }"
            class="governance-incident-link"
          >
            返回事故复盘
          </RouterLink>
        </div>
        <h3>{{ entry.action_item.title }}</h3>
        <p>{{ entry.action_item.description }}</p>
        <dl class="governance-action-facts">
          <div>
            <dt>负责人</dt>
            <dd>{{ entry.action_item.owner_id }}</dd>
          </div>
          <div>
            <dt>截止时间</dt>
            <dd>{{ formatTime(entry.action_item.due_at) }}</dd>
          </div>
          <div>
            <dt>来源服务</dt>
            <dd>{{ entry.incident.service_name }}</dd>
          </div>
          <div>
            <dt>事故标题</dt>
            <dd>{{ entry.incident.title }}</dd>
          </div>
        </dl>
      </li>
    </ol>

    <p v-if="total > items.length" class="governance-limit-note">
      当前只展示最早到期的 {{ items.length }} 项；请通过负责人或逾期条件缩小范围。
    </p>
  </section>
</template>
