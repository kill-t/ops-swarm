import type { Incident, IncidentPage } from "../types/incidents";
import type { EvaluationRun, EvaluationRunPage } from "../types/evaluations";
import type { EvidenceBoardSnapshot } from "../types/evidence-board";
import type { InvestigationAttemptList } from "../types/investigation-attempts";
import type { IncidentWorkroomSnapshot } from "../types/workroom";

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

async function requestJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(apiUrl(path), {
    headers: { Accept: "application/json" },
    signal,
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
  return requestJson<IncidentPage>("/api/v1/incidents?limit=100", signal);
}

export function getIncident(incidentId: string, signal?: AbortSignal): Promise<Incident> {
  return requestJson<Incident>(`/api/v1/incidents/${encodeURIComponent(incidentId)}`, signal);
}

export function getIncidentWorkroom(
  incidentId: string,
  signal?: AbortSignal,
): Promise<IncidentWorkroomSnapshot> {
  return requestJson<IncidentWorkroomSnapshot>(
    `/api/v1/incidents/${encodeURIComponent(incidentId)}/workroom`,
    signal,
  );
}

export function listInvestigationAttempts(
  runId: string,
  signal?: AbortSignal,
): Promise<InvestigationAttemptList> {
  return requestJson<InvestigationAttemptList>(
    `/api/v1/investigations/${encodeURIComponent(runId)}/attempts`,
    signal,
  );
}

export function getEvidenceBoard(
  runId: string,
  signal?: AbortSignal,
): Promise<EvidenceBoardSnapshot> {
  return requestJson<EvidenceBoardSnapshot>(
    `/api/v1/investigations/${encodeURIComponent(runId)}/evidence-board`,
    signal,
  );
}

export function listEvaluationRuns(
  beforeSequence?: number,
  signal?: AbortSignal,
): Promise<EvaluationRunPage> {
  const cursor = beforeSequence === undefined ? "" : `&before_sequence=${beforeSequence}`;
  return requestJson<EvaluationRunPage>(`/api/v1/evaluation-runs?limit=50${cursor}`, signal);
}

export function getEvaluationRun(runId: string, signal?: AbortSignal): Promise<EvaluationRun> {
  return requestJson<EvaluationRun>(
    `/api/v1/evaluation-runs/${encodeURIComponent(runId)}`,
    signal,
  );
}
