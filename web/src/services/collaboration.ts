import { requestJson } from "./api";

export interface Command {
  label: string;
  permission: string;
  path: string;
  body: Record<string, unknown>;
  input?: "statement" | "confirm" | "conditions";
  catalog?: ConditionItem[];
  preparesRelease?: boolean;
}

export interface ConditionItem {
  condition_id: string;
  text: string;
}

export interface CollaborationPanel {
  facts: { title: string; value: string }[];
  commands: Command[];
  tasks: { id: string; round: number; role: string; goal: string; status: string }[];
  runPath: string;
  pending: boolean;
}

interface Navigation {
  run_id: string;
  status: string;
  handoff: { id: string; source_sha256: string; destination: string } | null;
  handoff_sha256: string | null;
  preparation_id: string | null;
  review_job_id: string | null;
  review_route_sha256: string | null;
  candidate_id: string | null;
  review_rationale: string | null;
  experience_ids: string[] | null;
}

interface BoundRecord {
  id: string;
  content_sha256: string;
  incident_version: number;
}

interface RecordView<T> {
  record: T | null;
  availability: string;
}

export interface ReleasePreparation {
  request: RecordView<BoundRecord & { expires_at: string }>;
}

export function dispatchCommand(path: string, record: BoundRecord): Command {
  return {
    label: "派发演练回滚", permission: "execute_action", path: path + "/execution-job",
    body: {
      expected_incident_version: record.incident_version,
      request_id: record.id, request_content_sha256: record.content_sha256,
    },
    input: "confirm",
  };
}

interface Job {
  status: string;
  last_error_code: string | null;
  availability: string;
}

interface ConditionRecord extends BoundRecord {
  version: number;
  content: {
    status: string;
    items: { condition: ConditionItem; status: string; explanation: string }[];
  };
}

export async function loadCollaboration(
  incidentId: string,
  incidentVersion: number,
  signal: AbortSignal,
): Promise<CollaborationPanel | null> {
  const get = <T>(path: string) => requestJson<T>(path, { signal });
  const navigation = await get<Navigation | null>(
    `/api/v1/incidents/${encodeURIComponent(incidentId)}/collaboration-navigation`,
  );
  if (!navigation) return null;

  const { status, handoff } = navigation;
  const base = `/api/v1/investigations/${encodeURIComponent(navigation.run_id)}`;
  const panel: CollaborationPanel = {
    facts: [], commands: [], tasks: [], runPath: base,
    pending: status === "queued" || status === "running",
  };
  const fact = (title: string, value: string | null | undefined) => {
    if (value) panel.facts.push({ title, value });
  };
  const command = (
    label: string, permission: string, path: string, body: Command["body"],
    input?: Command["input"], catalog?: ConditionItem[],
  ) => panel.commands.push({ label, permission, path, body, input, catalog });
  const [collaboration, budget] = await Promise.all([
    get<{ rounds: {
      round_number: number;
      tasks: { id: string; assignment: { role: string; goal: string }; status: string }[];
    }[] }>(base + "/collaboration"),
    get<{
      policy: { max_model_calls: number; max_tool_calls: number };
      reserved_model_calls: number;
      reserved_tool_calls: number;
      deadline: string | null;
    }>(base + "/call-budget"),
  ]);
  fact("调查状态", status);
  fact("独立审查", navigation.review_rationale);
  fact("历史经验引用", navigation.experience_ids?.join("、") || "暂无引用");
  fact(
    "共享调用预算",
    `模型 ${budget.reserved_model_calls}/${budget.policy.max_model_calls} · 工具 ${budget.reserved_tool_calls}/${budget.policy.max_tool_calls} · 截止 ${budget.deadline ?? "尚未开始"}`,
  );
  panel.tasks = collaboration.rounds.flatMap((round) => round.tasks.map((task) => ({
    id: task.id, round: round.round_number, ...task.assignment, status: task.status,
  })));

  const writable = status === "await_candidate" || status === "await_human";
  let source = handoff?.destination === "candidate_evaluation" ? {
    source_kind: "terminal_handoff",
    source_id: handoff.id,
    source_sha256: navigation.handoff_sha256,
    expected_wait_version: null as number | null,
  } : null;
  if (status === "await_human" && handoff) {
    const path = base + "/human-wait";
    const wait = await get<{
      state_version: number;
      events: { id: string; command: { items?: { kind: string; text: string }[] } }[];
    }>(path);
    for (const event of wait.events) {
      for (const item of event.command.items ?? []) fact("人工补充 · " + item.kind, item.text);
    }
    const basis = {
      handoff_id: handoff.id,
      source_sha256: handoff.source_sha256,
      expected_wait_version: wait.state_version,
    };
    if (wait.state_version < 16 && !navigation.candidate_id) {
      command("提交人工补充", "manage_incident", path + "/supplements",
        { ...basis, action: "supplement" }, "statement");
    }
    if (!navigation.candidate_id) {
      command("取消本次调查", "manage_incident", path + "/cancel",
        { ...basis, action: "cancel" }, "confirm");
    }
    if (!navigation.preparation_id && wait.state_version > 1) {
      command("准备重评材料", "manage_incident", path + "/preparations",
        { ...basis, action: "prepare" });
    }
    if (navigation.preparation_id) {
      const preparationPath = path + "/preparations/" + navigation.preparation_id;
      const prepared = await get<Job & { artifact_sha256: string; availability_reason: string }>(
        preparationPath,
      );
      panel.pending ||= prepared.availability === "pending";
      fact("重评材料", prepared.availability_reason || prepared.last_error_code || prepared.status);
      if (prepared.availability === "ready" && !navigation.review_job_id) {
        command("提交独立重评", "manage_incident", preparationPath + "/reassessments",
          { ...basis, action: "reassess", preparation_sha256: prepared.artifact_sha256 });
      }
      if (navigation.review_job_id) {
        const reviewPath = preparationPath + "/reassessments/" + navigation.review_job_id;
        const review = await get<Job & { result: { content: { proposal: { rationale: string } } } | null }>(
          reviewPath,
        );
        panel.pending ||= review.availability === "pending";
        fact("人工重评", review.result?.content.proposal.rationale || review.last_error_code || review.status);
        if (review.status === "succeeded") {
          const route = await get<RecordView<{
            id: string; wait_version: number;
            decision: { route: string; blocking_reason_codes: string[] };
          }>>(reviewPath + "/route");
          panel.pending ||= route.availability === "pending";
          fact("重评去向", route.record?.decision.route);
          fact("重评阻断", route.record?.decision.blocking_reason_codes.join("、"));
          if (route.record?.decision.route === "candidate_evaluation" && route.availability === "current") {
            source = {
              source_kind: "human_route", source_id: route.record.id,
              source_sha256: navigation.review_route_sha256,
              expected_wait_version: route.record.wait_version,
            };
          }
        }
      }
    }
  }

  const candidates = base + "/candidate-evaluations";
  if (!navigation.candidate_id && writable && source?.source_sha256) {
    command("评估单步骤候选", "manage_incident", candidates, { ...source, action: "evaluate_candidate" });
  }
  if (navigation.candidate_id) {
    await loadDisposition(get, candidates + "/" + navigation.candidate_id, incidentVersion,
      writable, fact, command, panel);
  }
  if (status === "settled") {
    const convergence = await get<{ record: (BoundRecord & { outcome: string; after_version: number }) | null }>(
      base + "/incident-convergence",
    );
    panel.pending ||= !convergence.record;
    fact("事故收敛", convergence.record?.outcome);
    if (convergence.record?.outcome === "observed_failure") {
      const path = base + "/reinvestigation-admission";
      const admission = await get<RecordView<BoundRecord>>(path);
      if (!admission.record && convergence.record.after_version === incidentVersion) {
        command("申请一次失败回流", "manage_incident", path, {
          action: "request_reinvestigation", convergence_sha256: convergence.record.content_sha256,
          expected_incident_version: convergence.record.after_version,
        });
      }
      if (admission.record) {
        const route = await get<RecordView<{ route: string; blocking_reason_codes: string[] }>>(
          base + "/reinvestigation-routing",
        );
        panel.pending ||= route.availability === "pending";
        fact("失败回流交接", route.record?.route === "await_human" ? "等待工程师接管" : route.availability);
        fact("交接原因", route.record?.blocking_reason_codes.join("、"));
        if (!route.record) {
          const stages = await Promise.all(["coordination", "dispatch", "execution", "synthesis", "review"]
            .map((stage) => get<{
              status?: string; last_error_code?: string;
              record?: { status: string; reason_code: string };
              tasks?: { status: string; last_error_code?: string }[];
            }>(base + "/reinvestigation-" + stage)));
          stages.forEach((stage, index) => fact(
            ["回流协调", "回流分派", "回流取证", "回流综合", "回流独立审查"][index]!,
            stage.last_error_code ?? stage.record?.reason_code ?? stage.status ?? stage.record?.status
              ?? stage.tasks?.map((task) => task.last_error_code ?? task.status).join("、"),
          ));
        }
      }
    }
  }
  return panel;
}

async function loadDisposition(
  get: <T>(path: string) => Promise<T>,
  path: string,
  incidentVersion: number,
  writable: boolean,
  fact: (title: string, value: string | null | undefined) => void,
  command: (label: string, permission: string, path: string, body: Command["body"],
    input?: Command["input"], catalog?: ConditionItem[]) => void,
  panel: CollaborationPanel,
): Promise<void> {
  const job = await get<Job & {
    wait_version: number | null;
    result: { content_sha256: string; content: {
      disposition: string; step: { action_type: string } | null; proposal: { rationale: string };
    } } | null;
  }>(path);
  panel.pending ||= job.availability === "pending";
  fact("候选评估", job.result?.content.proposal.rationale || job.last_error_code || job.status);
  if (!job.result || job.result.content.disposition !== "planning_review") return;
  const editable = writable && job.availability === "current";
  const expected = { expected_incident_version: incidentVersion };
  const wait = { ...expected, expected_wait_version: job.wait_version };
  const planPath = path + "/action-plan";
  const plan = await get<RecordView<BoundRecord & {
    content: { preconditions: string[]; unresolved_questions: string[] };
  }>>(planPath);
  if (!plan.record) {
    if (editable) command("创建单步骤草案", "plan_action", planPath,
      { ...wait, action: "create_plan", candidate_result_sha256: job.result.content_sha256 });
    return;
  }
  fact("处置草案", job.result.content.step?.action_type);
  for (const item of plan.record.content.preconditions) fact("前置条件", item);
  for (const item of plan.record.content.unresolved_questions) fact("未解问题", item);
  const approvalPath = planPath + "/approval-request";
  const approval = await get<RecordView<BoundRecord & { plan_content_sha256: string }>>(approvalPath);
  if (!approval.record) {
    if (editable && plan.availability === "current") command("申请草案审批", "request_approval", approvalPath,
      { ...wait, action: "request_approval", plan_content_sha256: plan.record.content_sha256,
        acknowledge_unresolved_conditions: true }, "confirm");
    return;
  }
  const decision = await get<RecordView<BoundRecord & { outcome: string }>>(approvalPath + "/decision");
  fact("草案审批", decision.record?.outcome ?? approval.availability);
  if (!decision.record) {
    if (editable && approval.availability === "current") {
      for (const action of ["approve", "reject"]) command(
        action === "approve" ? "批准草案" : "拒绝草案", "decide_approval", approvalPath + "/decision", {
          ...expected, action, request_id: approval.record.id,
          plan_content_sha256: approval.record.plan_content_sha256,
          acknowledge_unresolved_conditions: action === "approve",
        }, action === "approve" ? "confirm" : undefined,
      );
    }
    return;
  }
  if (decision.record.outcome !== "approved") return;
  const conditionPath = path + "/execution-conditions";
  const conditions = await get<{
    catalog: ConditionItem[]; condition_version: number; latest: ConditionRecord | null;
  }>(conditionPath);
  const latest = conditions.latest;
  fact("条件核验", latest?.content.status ?? "待核验");
  for (const item of latest?.content.items ?? []) {
    fact(item.condition.text, `${item.status} · ${item.explanation}`);
  }
  if (editable && decision.availability === "current" && (!latest || latest.content.status === "blocked")) {
    command("提交逐条条件核验", "verify_action", conditionPath, {
      ...expected, decision_id: decision.record.id,
      decision_content_sha256: decision.record.content_sha256,
      expected_condition_version: conditions.condition_version, previous_content_sha256: latest?.content_sha256 ?? null,
    }, "conditions", conditions.catalog);
  }
  if (!latest || latest.content.status !== "ready_for_review") return;
  const versionPath = conditionPath + "/versions/" + latest.version;
  const conditionApprovalPath = versionPath + "/approval-request";
  const conditionRequest = await get<RecordView<BoundRecord>>(conditionApprovalPath);
  if (!conditionRequest.record) {
    if (editable && decision.availability === "current") command("申请核验审批", "request_approval", conditionApprovalPath, {
      ...expected, verification_id: latest.id, verification_content_sha256: latest.content_sha256,
    });
    return;
  }
  const conditionDecision = await get<RecordView<BoundRecord & { outcome: string }>>(
    conditionApprovalPath + "/decision",
  );
  fact("核验审批", conditionDecision.record?.outcome ?? conditionRequest.availability);
  if (!conditionDecision.record) {
    if (editable && conditionRequest.availability === "current") {
      for (const action of ["approve", "reject"]) command(
        action === "approve" ? "批准核验" : "拒绝核验", "decide_approval", conditionApprovalPath + "/decision", {
          ...expected, action, request_id: conditionRequest.record.id,
          request_content_sha256: conditionRequest.record.content_sha256,
          acknowledge_verification: action === "approve",
        }, action === "approve" ? "confirm" : undefined,
      );
    }
    return;
  }
  if (conditionDecision.record.outcome !== "approved") return;
  const releasePath = versionPath + "/release-execution-request";
  const release = await get<RecordView<BoundRecord & { expires_at: string }>>(releasePath);
  if (!release.record) {
    if (editable && conditionDecision.availability === "current") panel.commands.push({
      label: "准备演练回滚", permission: "execute_action", path: releasePath, input: "confirm",
      preparesRelease: true,
      body: {
        ...expected, approval_decision_id: conditionDecision.record.id,
        approval_content_sha256: conditionDecision.record.content_sha256,
      },
    });
    return;
  }
  fact("回滚准备有效期", release.record.expires_at);
  const executionPath = releasePath + "/execution-job";
  const execution = await get<{
    record: { status: string } | null; result: { outcome: string } | null;
  }>(executionPath);
  fact("受控执行", execution.result?.outcome ?? execution.record?.status ?? release.availability);
  if (!execution.record) {
    if (editable && release.availability === "current" && release.record.incident_version === incidentVersion) {
      panel.commands.push(dispatchCommand(releasePath, release.record));
    }
  } else if (execution.result) {
    const recovery = await get<{
      result: { outcome: string } | null;
      samples: { observation: { http_status: number } | null }[];
    }>(executionPath + "/recovery-verification");
    // 采样完成后仍需等父运行收尾，之后才可读取事故收敛决定。
    panel.pending ||= !recovery.result || writable;
    fact("客观恢复", recovery.result?.outcome ?? "待采样");
    fact("业务样本", recovery.samples.map((sample) => sample.observation?.http_status ?? "采样失败").join("、"));
  } else {
    panel.pending ||= execution.record.status === "queued" || execution.record.status === "running";
  }
}
