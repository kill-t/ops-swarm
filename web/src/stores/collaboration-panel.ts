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

export function useCollaborationPanel(scope: () => { id: string; version: number; cursor: number }) {
  const panel = shallowRef<CollaborationPanel | null>(null);
  const loading = ref(false);
  const error = ref("");
  const sending = ref(false);
  const notice = ref("");
  let reader: AbortController | undefined;
  let writer: AbortController | undefined;
  let pending: { fingerprint: string; body: Record<string, unknown> } | undefined;
  const commands = computed(() => {
    const principal = operatorIdentity.value;
    return principal && !error.value
      ? (panel.value?.commands ?? []).filter((command) => principal.permissions.includes(command.permission))
      : [];
  });

  async function refresh(): Promise<void> {
    reader?.abort();
    if (!operatorIdentity.value) return;
    const controller = new AbortController();
    reader = controller;
    loading.value = true;
    error.value = "";
    try {
      const incident = await getIncident(scope().id, controller.signal);
      const snapshot = await loadCollaboration(incident.id, incident.version, controller.signal);
      if (!controller.signal.aborted) panel.value = snapshot;
    } catch (cause) {
      if (!controller.signal.aborted) {
        error.value = cause instanceof Error ? cause.message : "协作状态读取失败";
      }
    } finally {
      if (!controller.signal.aborted) loading.value = false;
    }
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
      if (panel.value && prepared?.availability === "current" && prepared.record) {
        // 派发窗口较短，直接使用本次准备返回的权威版本及摘要。
        panel.value = {
          ...panel.value,
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
      if (!controller.signal.aborted) sending.value = false;
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
    if (!sending.value) void refresh();
  });
  onScopeDispose(() => {
    reader?.abort();
    writer?.abort();
  });
  return { panel, loading, error, commands, sending, notice, refresh, submit };
}
