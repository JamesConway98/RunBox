import type {
  ApiErrorBody,
  CreateRunRequest,
  Page,
  Run,
  RunCreated,
  TraceEvent,
} from "./types";

// Requests go to /api/*, which Next rewrites to the control plane. Same origin
// means EventSource works without CORS preflight and the API key never has to
// be exposed to the browser in a deployed setup.
const BASE = "/api";

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly detail: Record<string, unknown> | null;

  constructor(status: number, body: ApiErrorBody) {
    super(body.message);
    this.name = "ApiError";
    this.status = status;
    this.code = body.error;
    this.detail = body.detail ?? null;
  }
}

interface RequestOptions extends Omit<RequestInit, "body"> {
  body?: unknown;
  /** Aborts the request when the caller's controller fires. */
  signal?: AbortSignal;
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { body, headers, ...rest } = options;

  const response = await fetch(`${BASE}${path}`, {
    ...rest,
    headers: {
      ...(body === undefined ? {} : { "Content-Type": "application/json" }),
      ...headers,
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });

  if (!response.ok) {
    throw new ApiError(response.status, await readErrorBody(response));
  }

  if (response.status === 204) {
    return undefined as T;
  }
  return (await response.json()) as T;
}

// An error response that is not our error envelope — a proxy 502, an HTML error
// page — still has to produce something a UI can render.
async function readErrorBody(response: Response): Promise<ApiErrorBody> {
  try {
    const parsed = (await response.json()) as Partial<ApiErrorBody> & {
      detail?: ApiErrorBody | string;
    };
    if (parsed.detail && typeof parsed.detail === "object" && "message" in parsed.detail) {
      return parsed.detail as ApiErrorBody;
    }
    if (parsed.error && parsed.message) {
      return parsed as ApiErrorBody;
    }
    return {
      error: "unexpected_error",
      message: typeof parsed.detail === "string" ? parsed.detail : response.statusText,
    };
  } catch {
    return {
      error: "unexpected_error",
      message: `${response.status} ${response.statusText}`,
    };
  }
}

export const api = {
  createRun(body: CreateRunRequest, signal?: AbortSignal): Promise<RunCreated> {
    return request<RunCreated>("/v1/runs", { method: "POST", body, signal });
  },

  getRun(id: string, signal?: AbortSignal): Promise<Run> {
    return request<Run>(`/v1/runs/${id}`, { signal });
  },

  listRuns(
    params: { limit?: number; cursor?: string; status?: string; model?: string } = {},
    signal?: AbortSignal,
  ): Promise<Page<Run>> {
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(params)) {
      if (value !== undefined && value !== null && value !== "") {
        query.set(key, String(value));
      }
    }
    const suffix = query.size > 0 ? `?${query}` : "";
    return request<Page<Run>>(`/v1/runs${suffix}`, { signal });
  },

  listEvents(id: string, after = 0, signal?: AbortSignal): Promise<Page<TraceEvent>> {
    return request<Page<TraceEvent>>(`/v1/runs/${id}/events?after=${after}`, { signal });
  },

  cancelRun(id: string, signal?: AbortSignal): Promise<Run> {
    return request<Run>(`/v1/runs/${id}/cancel`, { method: "POST", signal });
  },

  /** URL for an EventSource. Resumption is a query parameter, not a body. */
  streamUrl(id: string, after = 0): string {
    return `${BASE}/v1/runs/${id}/stream${after > 0 ? `?after=${after}` : ""}`;
  },
};
