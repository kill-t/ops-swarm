<script setup lang="ts">
import { computed, reactive, ref, watch } from "vue";

import type {
  CreateReportActionItemInput,
  ReportActionItem,
} from "../types/report-action-items";

interface CompletionDraft {
  actorId: string;
  note: string;
}

const props = defineProps<{
  reportId: string;
  items: ReportActionItem[];
  total: number;
  loading: boolean;
  mutationLoading: boolean;
  error: string | null;
  message: string | null;
}>();

const emit = defineEmits<{
  refresh: [];
  create: [input: CreateReportActionItemInput];
  complete: [itemId: string, actorId: string, completionNote: string];
}>();

const title = ref("");
const description = ref("");
const ownerId = ref("");
const dueAt = ref("");
const actorId = ref("engineer-001");
const formError = ref<string | null>(null);
const submittedKey = ref<string | null>(null);
const completionDrafts = reactive<Record<string, CompletionDraft>>({});

function createIdempotencyKey(): string {
  if (typeof globalThis.crypto?.randomUUID === "function") {
    return `follow-up-${globalThis.crypto.randomUUID()}`;
  }
  return `follow-up-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

const idempotencyKey = ref(createIdempotencyKey());
const canCreate = computed(
  () =>
    !props.loading &&
    !props.mutationLoading &&
    title.value.trim().length > 0 &&
    description.value.trim().length > 0 &&
    ownerId.value.trim().length > 0 &&
    actorId.value.trim().length > 0 &&
    Number.isFinite(Date.parse(dueAt.value)),
);

watch([title, description, ownerId, dueAt, actorId], () => {
  idempotencyKey.value = createIdempotencyKey();
  submittedKey.value = null;
  formError.value = null;
});

watch(
  () => props.items.map((item) => `${item.id}:${item.status}`).join("|"),
  () => {
    for (const item of props.items) {
      if (item.status === "open" && completionDrafts[item.id] === undefined) {
        completionDrafts[item.id] = { actorId: "engineer-001", note: "" };
      }
      if (item.status === "completed") delete completionDrafts[item.id];
    }
    if (
      submittedKey.value !== null &&
      props.items.some((item) => item.idempotency_key === submittedKey.value)
    ) {
      title.value = "";
      description.value = "";
      ownerId.value = "";
      dueAt.value = "";
      submittedKey.value = null;
      idempotencyKey.value = createIdempotencyKey();
    }
  },
  { immediate: true },
);

watch(
  () => props.reportId,
  () => {
    title.value = "";
    description.value = "";
    ownerId.value = "";
    dueAt.value = "";
    actorId.value = "engineer-001";
    submittedKey.value = null;
    idempotencyKey.value = createIdempotencyKey();
    formError.value = null;
    for (const itemId of Object.keys(completionDrafts)) delete completionDrafts[itemId];
  },
);

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

function isOverdue(item: ReportActionItem): boolean {
  return item.status === "open" && Date.parse(item.due_at) < Date.now();
}

function submitCreation(): void {
  if (!canCreate.value) {
    formError.value = "请完整填写标题、说明、负责人、截止时间和操作者。";
    return;
  }
  const parsedDueAt = new Date(dueAt.value);
  if (!Number.isFinite(parsedDueAt.getTime())) {
    formError.value = "截止时间格式不正确。";
    return;
  }
  submittedKey.value = idempotencyKey.value;
  emit("create", {
    idempotency_key: idempotencyKey.value,
    title: title.value.trim(),
    description: description.value.trim(),
    owner_id: ownerId.value.trim(),
    due_at: parsedDueAt.toISOString(),
    actor_id: actorId.value.trim(),
  });
}

function canComplete(itemId: string): boolean {
  const draft = completionDrafts[itemId];
  return (
    draft !== undefined &&
    draft.actorId.trim().length > 0 &&
    draft.note.trim().length > 0 &&
    !props.loading &&
    !props.mutationLoading
  );
}

function submitCompletion(item: ReportActionItem): void {
  const draft = completionDrafts[item.id];
  if (draft === undefined || !canComplete(item.id)) return;
  emit("complete", item.id, draft.actorId.trim(), draft.note.trim());
}
</script>

<template>
  <section class="report-action-items-panel" aria-labelledby="report-action-items-title">
    <div class="section-heading">
      <div>
        <p class="eyebrow">REPORT FOLLOW-UP</p>
        <h3 id="report-action-items-title">复盘改进行动项</h3>
      </div>
      <button
        class="icon-button"
        type="button"
        :disabled="loading || mutationLoading"
        @click="emit('refresh')"
      >
        {{ loading ? "读取中" : `同步 · ${total}` }}
      </button>
    </div>

    <div class="follow-up-boundary">
      <strong>报告保持冻结，行动项独立推进</strong>
      <span>
        创建和完成必须由工程师提交。Agent 只能提供建议，不能分配负责人或关闭组织任务。
      </span>
    </div>

    <p v-if="message" class="follow-up-success" role="status">{{ message }}</p>
    <p v-if="error" class="follow-up-error" role="alert">{{ error }}</p>

    <form class="follow-up-create-form" @submit.prevent="submitCreation">
      <div class="follow-up-form-heading">
        <strong>登记改进工作</strong>
        <span>网络失败后内容不变时，表单会复用同一个幂等键。</span>
      </div>
      <label>
        标题
        <input v-model="title" name="action-title" maxlength="200" autocomplete="off" />
      </label>
      <label class="follow-up-wide-field">
        说明
        <textarea
          v-model="description"
          name="action-description"
          maxlength="1000"
          rows="3"
        />
      </label>
      <label>
        负责人
        <input v-model="ownerId" name="action-owner" maxlength="100" autocomplete="off" />
      </label>
      <label>
        截止时间
        <input v-model="dueAt" name="action-due-at" type="datetime-local" />
      </label>
      <label>
        创建操作者
        <input v-model="actorId" name="action-actor" maxlength="100" autocomplete="off" />
      </label>
      <p v-if="formError" class="follow-up-form-error" role="alert">{{ formError }}</p>
      <button
        class="decision-button create-follow-up"
        type="submit"
        :disabled="!canCreate"
      >
        {{ mutationLoading ? "提交中…" : "创建行动项" }}
      </button>
    </form>

    <p v-if="loading && items.length === 0" class="empty-state" aria-live="polite">
      正在读取复盘改进行动项…
    </p>
    <p v-else-if="items.length === 0" class="empty-state">
      当前报告还没有改进行动项。事故报告不会因此被改写。
    </p>
    <ol v-else class="follow-up-list">
      <li
        v-for="item in items"
        :key="item.id"
        class="follow-up-item"
        :class="{ completed: item.status === 'completed', overdue: isOverdue(item) }"
      >
        <div class="follow-up-item-heading">
          <div>
            <span class="follow-up-status">{{ item.status === "open" ? "待完成" : "已完成" }}</span>
            <strong>{{ item.title }}</strong>
          </div>
          <span>v{{ item.version }}</span>
        </div>
        <p>{{ item.description }}</p>
        <dl class="follow-up-facts">
          <div>
            <dt>负责人</dt>
            <dd>{{ item.owner_id }}</dd>
          </div>
          <div>
            <dt>截止时间</dt>
            <dd>{{ formatTime(item.due_at) }}<em v-if="isOverdue(item)">已逾期</em></dd>
          </div>
          <div>
            <dt>创建审计</dt>
            <dd>{{ item.created_by_id }} · {{ formatTime(item.created_at) }}</dd>
          </div>
        </dl>

        <div v-if="item.status === 'completed'" class="follow-up-completion-audit">
          <strong>{{ item.completion_note }}</strong>
          <span>{{ item.completed_by_id }} · {{ formatTime(item.completed_at ?? item.created_at) }}</span>
        </div>
        <form v-else class="follow-up-completion-form" @submit.prevent="submitCompletion(item)">
          <label>
            完成操作者
            <input
              v-model="completionDrafts[item.id].actorId"
              :name="`completion-actor-${item.id}`"
              maxlength="100"
              autocomplete="off"
            />
          </label>
          <label>
            完成说明
            <input
              v-model="completionDrafts[item.id].note"
              :name="`completion-note-${item.id}`"
              maxlength="500"
              autocomplete="off"
            />
          </label>
          <button
            class="decision-button complete-follow-up"
            type="submit"
            :disabled="!canComplete(item.id)"
          >
            {{ mutationLoading ? "提交中…" : "确认完成" }}
          </button>
        </form>
      </li>
    </ol>
  </section>
</template>
