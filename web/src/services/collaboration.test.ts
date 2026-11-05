import { afterEach, expect, it, vi } from "vitest";
import { loadCollaboration } from "./collaboration";
import { clearOperatorSession, setOperatorSession } from "./operator-session";

const run = "/api/v1/investigations/run";
const candidate = run + "/candidate-evaluations/candidate";
const conditions = candidate + "/execution-conditions";
const approval = conditions + "/versions/2/approval-request";
const release = conditions + "/versions/2/release-execution-request";

const navigation = {
  run_id: "run", status: "await_human",
  handoff: { id: "handoff", source_sha256: "route-source", destination: "await_human" },
  handoff_sha256: "handoff-hash", preparation_id: "prepare", review_job_id: "review",
  review_route_sha256: "full-review-route-hash", candidate_id: null,
  review_rationale: "已有独立审查", experience_ids: ["reviewed-experience"],
};
const wait = run + "/human-wait";
const prep = wait + "/preparations/prepare";
const review = prep + "/reassessments/review";
const routes: Record<string, unknown> = {
  [run + "/collaboration"]: { rounds: [] },
  [run + "/call-budget"]: {
    policy: { max_model_calls: 12, max_tool_calls: 10 },
    reserved_model_calls: 4, reserved_tool_calls: 1, deadline: null,
  },
  [wait]: { state_version: 2, events: [] },
  [prep]: { status: "succeeded", availability: "ready", artifact_sha256: "prepared" },
  [review]: { status: "succeeded" },
  [review + "/route"]: {
    record: { id: "review-route", wait_version: 2, decision: {
      route: "candidate_evaluation", blocking_reason_codes: [],
    } },
    availability: "current",
  },
};
const nav = "/api/v1/incidents/incident/collaboration-navigation";

function serve(responses: Record<string, unknown>): void {
  vi.stubGlobal("fetch", vi.fn(async (path: string, options: RequestInit) => {
    expect(options.headers).toMatchObject({ Authorization: "Bearer test-only-credential" });
    if (!(path in responses)) throw new Error("意外路径 " + path);
    return Response.json(responses[path]);
  }));
  setOperatorSession("test-only-credential", {
    credential_id: "operator", subject_id: "operator", role: "viewer", permissions: ["read"],
    expires_at: new Date(Date.now() + 30_000).toISOString(),
  });
}

afterEach(() => {
  clearOperatorSession();
  vi.unstubAllGlobals();
});

it("刷新时从持久导航恢复人工重评与精确候选依据", async () => {
  serve({ ...routes, [nav]: navigation });
  const panel = await loadCollaboration("incident", 7, new AbortController().signal);
  expect(panel?.commands).toContainEqual(expect.objectContaining({
    label: "评估单步骤候选", path: run + "/candidate-evaluations",
    body: { action: "evaluate_candidate", source_kind: "human_route", source_id: "review-route",
      source_sha256: "full-review-route-hash", expected_wait_version: 2 },
  }));
  expect(panel?.facts).toContainEqual({ title: "历史经验引用", value: "reviewed-experience" });
});

it("核验审批与派发分别绑定当前版本的不同权威依据", async () => {
  const record = (id: string) => ({ id, content_sha256: id + "-hash", incident_version: 7 });
  const responses = {
    ...routes,
    [nav]: { ...navigation, candidate_id: "candidate" },
    [candidate]: { status: "succeeded", availability: "current", wait_version: 2, result: {
      content: { disposition: "planning_review", step: { action_type: "rollback_release" },
        proposal: { rationale: "回滚单步候选" } },
    } },
    [candidate + "/action-plan"]: { record: {
      ...record("plan"), content: { preconditions: [], unresolved_questions: [] },
    } },
    [candidate + "/action-plan/approval-request"]: { record: record("draft-request") },
    [candidate + "/action-plan/approval-request/decision"]: {
      availability: "current", record: { ...record("draft-decision"), outcome: "approved" },
    },
    [conditions]: { condition_version: 2, latest: {
      ...record("verification"), version: 2, content: { status: "ready_for_review", items: [] },
    } },
    [approval]: { record: record("verification-request"), availability: "current" },
  };
  serve({ ...responses, [approval + "/decision"]: { record: null } });
  const approve = await loadCollaboration("incident", 7, new AbortController().signal);
  expect(approve?.commands).toContainEqual(expect.objectContaining({
    label: "批准核验", permission: "decide_approval", path: approval + "/decision", input: "confirm",
    body: { action: "approve", expected_incident_version: 7, acknowledge_verification: true,
      request_id: "verification-request", request_content_sha256: "verification-request-hash" },
  }));
  serve({
    ...responses,
    [approval + "/decision"]: { record: { ...record("final-approval"), outcome: "approved" } },
    [release]: { availability: "current", record: record("dispatch-request") },
    [release + "/execution-job"]: { record: null, result: null },
  });
  const dispatch = await loadCollaboration("incident", 7, new AbortController().signal);
  expect(dispatch?.commands).toContainEqual(expect.objectContaining({
    label: "派发演练回滚", permission: "execute_action", path: release + "/execution-job", input: "confirm",
    body: { request_id: "dispatch-request", request_content_sha256: "dispatch-request-hash",
      expected_incident_version: 7 },
  }));
});
