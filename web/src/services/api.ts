import type { CloseIncidentInput, Incident, IncidentPage } from "../types/incidents";
import type {
  ApprovalDecision,
  ApprovalDecisionInput,
  ApprovalDecisionResponse,
  PendingApprovalPage,
} from "../types/approvals";
import type { EvaluationRun, EvaluationRunPage } from "../types/evaluations";
import type {
  ExecuteOperationInput,
  ExecuteOperationResponse,
  ExecutionCandidatePage,
} from "../types/executions";
import type { EvidenceBoardSnapshot } from "../types/evidence-board";
import type { InvestigationAttemptList } from "../types/investigation-attempts";
import type {
  CreateIncidentReportInput,
  CreateIncidentReportResponse,
  IncidentReport,
} from "../types/reports";
import type { IncidentWorkroomSnapshot } from "../types/workroom";
import type {
  CreateVerificationRunInput,
  CreateVerificationRunResponse,
  VerificationCandidatePage,
  VerificationRun,
  VerificationWorkItem,
} from "../types/verifications";

interface ApiErrorBody {
  error_code?: string;
  message?: string;
}

const configuredBaseUrl = import.meta.env.VITE_API_BASE_URL?.trim() ?? "";
const apiBaseUrl = configuredBaseUrl.replace(/\/$/, "");

export class ApiRequestError extends Error {
  readonly status: number;
  readonly errorCode: string | null;

  constructor(message: string, status: number, errorCode: string | null) {
    super(message);
    this.name = "ApiRequestError";
    this.status = status;
    this.errorCode = errorCode;
  }
}

export function apiUrl(path: string): string {
  return `${apiBaseUrl}${path}`;
}

interface JsonRequestOptions {
  method?: "GET" | "POST";
  body?: object;
  signal?: AbortSignal;
}

async function requestJson<T>(path: string, options: JsonRequestOptions = {}): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (options.body !== undefined) {
    headers["Content-Type"] = "application/json";
  }
  const response = await fetch(apiUrl(path), {
    method: options.method ?? "GET",
    headers,
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
    signal: options.signal,
  });
  if (!response.ok) {
    let body: ApiErrorBody = {};
    try {
      body = (await response.json()) as ApiErrorBody;
    } catch {
      // 非 JSON 网关错误仍保留 HTTP 状态，不向页面暴露原始响应正文。
    }
    throw new ApiRequestError(
      body.message ?? `请求失败，HTTP ${response.status}`,
      response.status,
      body.error_code ?? null,
    );
  }
  return (await response.json()) as T;
}

export function listIncidents(signal?: AbortSignal): Promise<IncidentPage> {
  return requestJson<IncidentPage>("/api/v1/incidents?limit=100", { signal });
}

export function getIncident(incidentId: string, signal?: AbortSignal): Promise<Incident> {
  return requestJson<Incident>(`/api/v1/incidents/${encodeURIComponent(incidentId)}`, { signal });
}

export function closeIncident(
  incidentId: string,
  input: CloseIncidentInput,
  signal?: AbortSignal,
): Promise<Incident> {
  return requestJson<Incident>(
    `/api/v1/incidents/${encodeURIComponent(incidentId)}/actions/close`,
    { method: "POST", body: input, signal },
  );
}

export function getIncidentWorkroom(
  incidentId: string,
  signal?: AbortSignal,
): Promise<IncidentWorkroomSnapshot> {
  return requestJson<IncidentWorkroomSnapshot>(
    `/api/v1/incidents/${encodeURIComponent(incidentId)}/workroom`,
    { signal },
  );
}

export function getIncidentReport(
  incidentId: string,
  signal?: AbortSignal,
): Promise<IncidentReport> {
  return requestJson<IncidentReport>(
    `/api/v1/incidents/${encodeURIComponent(incidentId)}/reports/current`,
    { signal },
  );
}

export function createIncidentReport(
  incidentId: string,
  input: CreateIncidentReportInput,
  signal?: AbortSignal,
): Promise<CreateIncidentReportResponse> {
  return requestJson<CreateIncidentReportResponse>(
    `/api/v1/incidents/${encodeURIComponent(incidentId)}/reports`,
    { method: "POST", body: input, signal },
  );
}

export function listInvestigationAttempts(
  runId: string,
  signal?: AbortSignal,
): Promise<InvestigationAttemptList> {
  return requestJson<InvestigationAttemptList>(
    `/api/v1/investigations/${encodeURIComponent(runId)}/attempts`,
    { signal },
  );
}

export function getEvidenceBoard(
  runId: string,
  signal?: AbortSignal,
): Promise<EvidenceBoardSnapshot> {
  return requestJson<EvidenceBoardSnapshot>(
    `/api/v1/investigations/${encodeURIComponent(runId)}/evidence-board`,
    { signal },
  );
}

export function listEvaluationRuns(
  beforeSequence?: number,
  signal?: AbortSignal,
): Promise<EvaluationRunPage> {
  const cursor = beforeSequence === undefined ? "" : `&before_sequence=${beforeSequence}`;
  return requestJson<EvaluationRunPage>(`/api/v1/evaluation-runs?limit=50${cursor}`, { signal });
}

export function getEvaluationRun(runId: string, signal?: AbortSignal): Promise<EvaluationRun> {
  return requestJson<EvaluationRun>(
    `/api/v1/evaluation-runs/${encodeURIComponent(runId)}`,
    { signal },
  );
}

export function listPendingApprovals(signal?: AbortSignal): Promise<PendingApprovalPage> {
  return requestJson<PendingApprovalPage>("/api/v1/approval-requests?limit=50", { signal });
}

export function decideApprovalRequest(
  requestId: string,
  decision: ApprovalDecision,
  input: ApprovalDecisionInput,
  signal?: AbortSignal,
): Promise<ApprovalDecisionResponse> {
  return requestJson<ApprovalDecisionResponse>(
    `/api/v1/approval-requests/${encodeURIComponent(requestId)}/actions/${decision}`,
    { method: "POST", body: input, signal },
  );
}

export function listExecutionCandidates(signal?: AbortSignal): Promise<ExecutionCandidatePage> {
  return requestJson<ExecutionCandidatePage>("/api/v1/execution-candidates?limit=50", { signal });
}

export function executeActionStep(
  stepId: string,
  input: ExecuteOperationInput,
  signal?: AbortSignal,
): Promise<ExecuteOperationResponse> {
  return requestJson<ExecuteOperationResponse>(
    `/api/v1/action-steps/${encodeURIComponent(stepId)}/executions`,
    { method: "POST", body: input, signal },
  );
}

export function listVerificationCandidates(
  signal?: AbortSignal,
): Promise<VerificationCandidatePage> {
  return requestJson<VerificationCandidatePage>("/api/v1/verification-candidates?limit=50", {
    signal,
  });
}

export function createVerificationRun(
  executionId: string,
  input: CreateVerificationRunInput,
  signal?: AbortSignal,
): Promise<CreateVerificationRunResponse> {
  return requestJson<CreateVerificationRunResponse>(
    `/api/v1/operation-executions/${encodeURIComponent(executionId)}/verification-runs`,
    { method: "POST", body: input, signal },
  );
}

export function getVerificationRun(
  runId: string,
  signal?: AbortSignal,
): Promise<VerificationRun> {
  return requestJson<VerificationRun>(
    `/api/v1/verification-runs/${encodeURIComponent(runId)}`,
    { signal },
  );
}

export function getVerificationWorkItem(
  runId: string,
  signal?: AbortSignal,
): Promise<VerificationWorkItem> {
  return requestJson<VerificationWorkItem>(
    `/api/v1/verification-runs/${encodeURIComponent(runId)}/work-item`,
    { signal },
  );
}
