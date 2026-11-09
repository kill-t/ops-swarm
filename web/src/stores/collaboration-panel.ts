import { computed, onScopeDispose, ref, shallowRef, watch } from "vue";
import { getIncident, requestJson } from "../services/api";
import {
  dispatchCommand, loadCollaboration, type CollaborationPanel, type Command, type ReleasePreparation,
} from "../services/collaboration";
import { operatorIdentity } from "../services/operator-session";

export interface ConditionAnswer {
  condition_id: string;
  status: string;
  explanation: string;
}

const RECHECK_INTERVAL_MS = 2_000;
const RECHECK_WINDOW_MS = 60_000;

export function useCollaborationPanel(scope: () => { id: string; version: number; cursor: number }) {
  const panel = shallowRef<CollaborationPanel | null>(null);
  const loading = ref(false);
  const error = ref("");
  const sending = ref(false);
  const notice = ref("");
  let reader: AbortController | undefined;
  let writer: AbortController | undefined;
  let recheckTimer: ReturnType<typeof setTimeout> | undefined;
  let recheckUntil = 0;
  let refreshQueued = false;
  let pending: { fingerprint: string; body: Record<string, unknown> } | undefined;
  const commands = computed(() => {
    const principal = operatorIdentity.value;
    return principal && !error.value
      ? (panel.value?.commands ?? []).filter((command) => principal.permissions.includes(command.permission))
      : [];
  });

  async function readSnapshot(): Promise<void> {
    clearTimeout(recheckTimer);
    reader?.abort();
    if (!operatorIdentity.value) return;
    const controller = new AbortController();
    reader = controller;
    loading.value = true;
    error.value = "";
    try {
      const incident = await getIncident(scope().id, controller.signal);
      const snapshot = await loadCollaboration(incident.id, incident.version, controller.signal);
      if (controller.signal.aborted) return;
      if (snapshot && panel.value?.runPath === snapshot.runPath) {
        // 表单绑定具体命令。只有完整依据相同才保留该命令与输入。
        const previous = new Map(panel.value.commands.map((item) => [JSON.stringify(item), item]));
        panel.value = {
          ...snapshot,
          commands: snapshot.commands.map((item) => previous.get(JSON.stringify(item)) ?? item),
        };
      } else {
        panel.value = snapshot;
      }
    } catch (cause) {
      if (!controller.signal.aborted) {
        error.value = cause instanceof Error ? cause.message : "协作状态读取失败";
      }
    } finally {
      if (!controller.signal.aborted) loading.value = false;
    }
  }

  function refresh(): Promise<void> {
    refreshQueued = false;
    recheckUntil = performance.now() + RECHECK_WINDOW_MS;
    return readSnapshot();
  }

  async function submit(
    command: Command,
    reason: string,
    answers: ConditionAnswer[],
    acknowledged: boolean,
  ): Promise<boolean> {
    if (sending.value || loading.value || !commands.value.includes(command) || !reason.trim()) return false;
    if (command.input === "confirm" && !acknowledged) return false;
    const fingerprint = JSON.stringify([command.path, command.body, reason, answers]);
    if (pending?.fingerprint !== fingerprint) {
      const time = new Date().toISOString();
      const items = command.input === "conditions" ? answers.map((item) => ({
        ...item,
        sources: item.status === "unresolved" ? [] : [{
          kind: "human_observation", text: item.explanation, observed_at: time,
        }],
      })) : command.input === "statement" ? [{ kind: "statement", text: reason }] : undefined;
      pending = {
        fingerprint,
        body: { ...command.body, reason, idempotency_key: crypto.randomUUID(), ...(items ? { items } : {}) },
      };
    }
    const controller = new AbortController();
    writer = controller;
    sending.value = true;
    notice.value = "";
    try {
      const result = await requestJson<ReleasePreparation | undefined>(command.path, {
        method: "POST", body: pending.body, signal: controller.signal,
      });
      if (controller.signal.aborted) return false;
      pending = undefined;
      notice.value = "命令已记录";
      const prepared = command.preparesRelease ? result?.request : undefined;
      if (panel.value && prepared?.availability === "current" && prepared.record
          && prepared.record.incident_version >= scope().version) {
        // 派发窗口较短，直接使用本次准备返回的权威版本及摘要。
        panel.value = {
          ...panel.value,
          pending: false,
          facts: [...panel.value.facts, { title: "回滚准备有效期", value: prepared.record.expires_at }],
          commands: [dispatchCommand(command.path, prepared.record)],
        };
      } else {
        await refresh();
      }
      return true;
    } catch (cause) {
      if (!controller.signal.aborted) {
        notice.value = cause instanceof Error ? cause.message : "命令提交失败";
      }
      return false;
    } finally {
      if (!controller.signal.aborted) {
        sending.value = false;
        if (refreshQueued) void refresh();
      }
    }
  }

  watch([() => scope().id, operatorIdentity], () => {
    reader?.abort();
    writer?.abort();
    panel.value = null;
    loading.value = sending.value = false;
    error.value = notice.value = "";
    pending = undefined;
    void refresh();
  }, { immediate: true });
  watch([() => scope().version, () => scope().cursor], () => {
    if (sending.value) refreshQueued = true;
    else void refresh();
  });
  watch([panel, loading, sending, error], () => {
    clearTimeout(recheckTimer);
    if (!panel.value?.pending || loading.value || sending.value || error.value) return;
    if (performance.now() + RECHECK_INTERVAL_MS > recheckUntil) return;
    recheckTimer = setTimeout(() => {
      if (performance.now() <= recheckUntil) void readSnapshot();
    }, RECHECK_INTERVAL_MS);
  });
  onScopeDispose(() => {
    clearTimeout(recheckTimer);
    reader?.abort();
    writer?.abort();
  });
  return { panel, loading, error, commands, sending, notice, refresh, submit };
}
