import { flushPromises, mount } from "@vue/test-utils";
import { afterEach, expect, it, vi } from "vitest";
import TrustedCollaborationPanel from "./TrustedCollaborationPanel.vue";
import { clearOperatorSession, setOperatorSession } from "../services/operator-session";

const mocks = vi.hoisted(() => ({ load: vi.fn(), post: vi.fn() }));
vi.mock("../services/collaboration", async (original) => ({
  ...await original<typeof import("../services/collaboration")>(),
  loadCollaboration: mocks.load,
}));
vi.mock("../services/api", () => ({
  getIncident: vi.fn().mockResolvedValue({ id: "incident", version: 5 }),
  requestJson: mocks.post,
}));

afterEach(() => {
  clearOperatorSession();
  mocks.load.mockReset();
  mocks.post.mockReset();
});

it("仅显示角色获准的操作，失败重试保留原命令依据及幂等键", async () => {
  setOperatorSession("test-only-credential", {
    credential_id: "engineer", subject_id: "engineer", role: "engineer",
    permissions: ["read", "manage_incident"],
    expires_at: new Date(Date.now() + 30_000).toISOString(),
  });
  mocks.load.mockResolvedValue({
    facts: [{ title: "交接", value: "等待工程师接管" }], tasks: [], runPath: "/run",
    commands: [
      { label: "提交人工补充", permission: "manage_incident", path: "/supplements",
        body: { expected_wait_version: 2 }, input: "statement" },
      { label: "派发回滚", permission: "execute_action", path: "/dispatch", body: {} },
    ],
  });
  mocks.post.mockRejectedValueOnce(new Error("连接中断")).mockResolvedValueOnce({});
  const view = mount(TrustedCollaborationPanel, {
    props: { incidentId: "incident", version: 5, cursor: 0 },
  });
  try {
    await flushPromises();
    expect(view.text()).toContain("等待工程师接管");
    expect(view.text()).not.toContain("派发回滚");
    await view.findAll("button").find((button) => button.text() === "提交人工补充")!.trigger("click");
    await view.get("textarea").setValue("核对现场版本");
    await view.get("form").trigger("submit");
    await flushPromises();
    expect(view.text()).toContain("连接中断");
    await view.get("form").trigger("submit");
    await flushPromises();
    const first = mocks.post.mock.calls[0]?.[1].body;
    expect(mocks.post.mock.calls[1]?.[1].body).toEqual(first);
    expect(first).toMatchObject({
      expected_wait_version: 2, reason: "核对现场版本",
      idempotency_key: expect.any(String), items: [{ kind: "statement", text: "核对现场版本" }],
    });
  } finally {
    view.unmount();
  }
});

it("短执行窗口直接使用准备响应，仍要求独立派发确认", async () => {
  setOperatorSession("test-only-credential", {
    credential_id: "executor", subject_id: "executor", role: "executor",
    permissions: ["read", "execute_action"],
    expires_at: new Date(Date.now() + 30_000).toISOString(),
  });
  mocks.load.mockResolvedValue({
    facts: [], tasks: [], runPath: "/run",
    commands: [{ label: "准备演练回滚", permission: "execute_action", path: "/release",
      body: { approval_decision_id: "approval" }, input: "confirm", preparesRelease: true }],
  });
  mocks.post.mockResolvedValueOnce({ request: { availability: "current", record: {
    id: "release", incident_version: 5, content_sha256: "release-hash", expires_at: "2026-11-05T09:00:30Z",
  } } });
  const view = mount(TrustedCollaborationPanel, {
    props: { incidentId: "incident", version: 5, cursor: 0 },
  });
  try {
    await flushPromises();
    await view.findAll("button").find((button) => button.text() === "准备演练回滚")!.trigger("click");
    await view.get("textarea").setValue("确认准备");
    await view.get("input[type=checkbox]").setValue(true);
    await view.get("form").trigger("submit");
    await flushPromises();
    expect(mocks.load).toHaveBeenCalledTimes(1);
    expect(mocks.post).toHaveBeenCalledTimes(1);
    await view.findAll("button").find((button) => button.text() === "派发演练回滚")!.trigger("click");
    expect((view.get("input[type=checkbox]").element as HTMLInputElement).checked).toBe(false);
    expect(mocks.post).toHaveBeenCalledTimes(1);
  } finally {
    view.unmount();
  }
});

function mountEngineerPanel() {
  setOperatorSession("test-only-credential", {
    credential_id: "engineer", subject_id: "engineer", role: "engineer",
    permissions: ["read", "manage_incident"],
    expires_at: new Date(Date.now() + 120_000).toISOString(),
  });
  return mount(TrustedCollaborationPanel, {
    props: { incidentId: "incident", version: 5, cursor: 0 },
  });
}

const supplement = {
  label: "提交人工补充", permission: "manage_incident", path: "/supplements",
  body: { expected_wait_version: 1 }, input: "statement",
};
const waiting = { facts: [], tasks: [], runPath: "/run", commands: [supplement], pending: false };

it("人工补充成功后，读快照期间收到的新事件仍会刷新出准备命令", async () => {
  let finishRead!: (snapshot: typeof waiting) => void;
  const reading = new Promise<typeof waiting>((resolve) => { finishRead = resolve; });
  mocks.load.mockResolvedValueOnce(waiting).mockReturnValueOnce(reading).mockResolvedValueOnce({
    ...waiting,
    commands: [{ label: "准备重评材料", permission: "manage_incident", path: "/preparations",
      body: { expected_wait_version: 2 } }],
  });
  mocks.post.mockResolvedValueOnce({ applied: true, event: { state_version: 2 } });
  const view = mountEngineerPanel();
  try {
    await flushPromises();
    await view.findAll("button").find((button) => button.text() === "提交人工补充")!.trigger("click");
    await view.get("textarea").setValue("核对订单失败");
    await view.get("form").trigger("submit");
    await flushPromises();
    expect(mocks.load).toHaveBeenCalledTimes(2);
    await view.setProps({ cursor: 1 });
    finishRead(waiting);
    await flushPromises();
    expect(view.text()).toContain("准备重评材料");
    expect(mocks.post).toHaveBeenCalledTimes(1);
  } finally {
    view.unmount();
  }
});

it("后台准备就绪时自动显示重评入口，并保留同一依据下输入中的补充", async () => {
  vi.useFakeTimers();
  const pending = { ...waiting, pending: true };
  mocks.load.mockResolvedValueOnce(pending).mockResolvedValueOnce({
    ...waiting,
    commands: [
      structuredClone(supplement),
      { label: "提交独立重评", permission: "manage_incident", path: "/reassessments", body: {} },
    ],
  });
  const view = mountEngineerPanel();
  try {
    await flushPromises();
    await view.findAll("button").find((button) => button.text() === "提交人工补充")!.trigger("click");
    await view.get("textarea").setValue("尚未提交的文字");
    await vi.advanceTimersByTimeAsync(2_000);
    expect(view.text()).toContain("提交独立重评");
    expect((view.get("textarea").element as HTMLTextAreaElement).value).toBe("尚未提交的文字");
    await vi.advanceTimersByTimeAsync(4_000);
    expect(mocks.load).toHaveBeenCalledTimes(2);
    expect(mocks.post).not.toHaveBeenCalled();
  } finally {
    view.unmount();
    vi.useRealTimers();
  }
});

it("后台持续等待时停止有界补读，手动刷新仍可读取新依据", async () => {
  vi.useFakeTimers();
  mocks.load.mockResolvedValue({ ...waiting, pending: true });
  const view = mountEngineerPanel();
  try {
    await flushPromises();
    await view.findAll("button").find((button) => button.text() === "提交人工补充")!.trigger("click");
    await view.get("textarea").setValue("旧等待版本");
    await vi.advanceTimersByTimeAsync(60_000);
    const reads = mocks.load.mock.calls.length;
    expect(reads).toBeGreaterThan(1);
    await vi.advanceTimersByTimeAsync(4_000);
    expect(mocks.load).toHaveBeenCalledTimes(reads);
    mocks.load.mockResolvedValueOnce({ ...waiting, commands: [
      { ...supplement, body: { expected_wait_version: 2 } },
    ] });
    await view.get("header button").trigger("click");
    await flushPromises();
    expect(mocks.load).toHaveBeenCalledTimes(reads + 1);
    expect(view.find("form").exists()).toBe(false);
  } finally {
    view.unmount();
    vi.useRealTimers();
  }
});
