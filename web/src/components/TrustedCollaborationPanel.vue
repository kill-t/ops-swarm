<script setup lang="ts">
import { onScopeDispose, ref, shallowRef } from "vue";
import { requestJson } from "../services/api";
import type { Command } from "../services/collaboration";
import { useCollaborationPanel, type ConditionAnswer } from "../stores/collaboration-panel";

const props = defineProps<{ incidentId: string; version: number; cursor: number }>();
const { panel, loading, error, commands, sending, notice, refresh, submit } = useCollaborationPanel(
  () => ({ id: props.incidentId, version: props.version, cursor: props.cursor }),
);
const active = shallowRef<Command | null>(null);
const reason = ref("");
const confirmed = ref(false);
const answers = ref<ConditionAnswer[]>([]);
const evidence = ref<{ evidence_id: string; source: string; summary: string; observed_at: string }[]>([]);
const evidenceMessage = ref("");
let evidenceReader: AbortController | undefined;

function select(command: Command): void {
  active.value = command;
  reason.value = "";
  confirmed.value = false;
  answers.value = (command.catalog ?? []).map((item) => ({
    condition_id: item.condition_id, status: "unresolved", explanation: "",
  }));
}

async function confirmCommand(): Promise<void> {
  if (active.value && await submit(active.value, reason.value.trim(), answers.value, confirmed.value)) {
    active.value = null;
    reason.value = "";
  }
}

async function showEvidence(taskId: string): Promise<void> {
  if (!panel.value) return;
  evidenceReader?.abort();
  const controller = new AbortController();
  evidenceReader = controller;
  evidence.value = [];
  evidenceMessage.value = "正在读取任务证据";
  try {
    const result = await requestJson<{ snapshot: { evidence: typeof evidence.value } } | null>(
      panel.value.runPath + "/collaboration/tasks/" + encodeURIComponent(taskId) + "/evidence",
      { signal: controller.signal },
    );
    if (!controller.signal.aborted) {
      evidence.value = result?.snapshot.evidence ?? [];
      evidenceMessage.value = evidence.value.length ? "" : "该任务暂无证据";
    }
  } catch (cause) {
    if (!controller.signal.aborted) evidenceMessage.value = cause instanceof Error ? cause.message : "证据读取失败";
  }
}

onScopeDispose(() => evidenceReader?.abort());
</script>

<template>
  <section class="trusted-collaboration" aria-label="多 Agent 可信协作台">
    <header>
      <h2>多 Agent 可信协作台</h2>
      <button type="button" :disabled="loading || sending" @click="refresh">刷新协作状态</button>
    </header>
    <p v-if="loading" role="status">正在读取持久协作状态…</p>
    <p v-if="error" role="alert">{{ error }}</p>
    <p v-else-if="!panel && !loading">当前事故暂无现场动态调查。</p>
    <template v-if="panel">
      <dl>
        <div v-for="(fact, index) in panel.facts" :key="index">
          <dt>{{ fact.title }}</dt><dd>{{ fact.value }}</dd>
        </div>
      </dl>
      <h3>调查轮次与任务</h3>
      <ul>
        <li v-for="task in panel.tasks" :key="task.id">
          第 {{ task.round }} 轮 · {{ task.role }} · {{ task.status }} · {{ task.goal }}
          <button type="button" @click="showEvidence(task.id)">查看证据</button>
        </li>
      </ul>
      <p v-if="evidenceMessage" role="status">{{ evidenceMessage }}</p>
      <ul v-if="evidence.length">
        <li v-for="item in evidence" :key="item.evidence_id">
          {{ item.summary }} · {{ item.source }} · {{ item.observed_at }} · {{ item.evidence_id }}
        </li>
      </ul>
      <div class="collaboration-commands" aria-label="当前可用操作">
        <button v-for="item in commands" :key="item.label" type="button"
          :disabled="loading || sending" @click="select(item)">{{ item.label }}</button>
      </div>
      <form v-if="active && commands.includes(active)" @submit.prevent="confirmCommand">
        <h3>{{ active.label }}</h3>
        <fieldset :disabled="loading || sending">
          <label>{{ active.input === "statement" ? "人工说明" : "操作理由" }}
            <textarea v-model="reason" required maxlength="500" />
          </label>
          <label v-if="active.input === 'confirm'">
            <input v-model="confirmed" type="checkbox" required /> 已阅读当前依据，确认此操作
          </label>
          <div v-for="(item, index) in answers" :key="item.condition_id">
            <label>{{ active.catalog?.[index]?.text }}
              <select v-model="item.status">
                <option value="unresolved">尚未核实</option>
                <option value="verified">已核实</option>
                <option value="not_applicable">不适用</option>
              </select>
            </label>
            <label>核验说明或人工观察
              <input v-model.trim="item.explanation" required maxlength="500" />
            </label>
          </div>
          <button type="submit">确认提交</button>
        </fieldset>
      </form>
      <p v-if="notice" role="status">{{ notice }}</p>
    </template>
  </section>
</template>

<style scoped>
.trusted-collaboration { margin-block: 1rem; padding: 1rem; border: 1px solid currentColor; border-radius: .75rem; }
header, .collaboration-commands { display: flex; flex-wrap: wrap; gap: .75rem; align-items: center; }
header { justify-content: space-between; }
dl { display: grid; gap: .5rem; }
dt { font-weight: 600; }
dd { margin-inline-start: 0; overflow-wrap: anywhere; }
label { display: block; margin-block: .5rem; }
textarea { display: block; width: 100%; }
</style>
