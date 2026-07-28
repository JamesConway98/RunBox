import { readProviderKey } from "./useProviderKey";
import type {
  Batch,
  BatchDetail,
  Dataset,
  DatasetCase,
  EvalScore,
  ScoreBatchResponse,
} from "./batchTypes";
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
  /**
   * Attach the visitor's model provider key. Only run-creating calls set this;
   * a read of the runs list has no business carrying a credential.
   */
  withProviderKey?: boolean;
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { body, headers, withProviderKey, ...rest } = options;

  const providerKey = withProviderKey ? readProviderKey() : null;

  const response = await fetch(`${BASE}${path}`, {
    ...rest,
    headers: {
      ...(body === undefined ? {} : { "Content-Type": "application/json" }),
      ...(providerKey ? { "X-Provider-Key": providerKey } : {}),
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
    return request<RunCreated>("/v1/runs", {
      method: "POST",
      body,
      signal,
      withProviderKey: true,
    });
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

/* -------------------------------------------------------------------------- */
/* Datasets, batches and evals                                                */
/* -------------------------------------------------------------------------- */

export const datasetsApi = {
  list(signal?: AbortSignal): Promise<Page<Dataset>> {
    return request<Page<Dataset>>("/v1/datasets", { signal });
  },

  /**
   * Upload via FormData, so the Content-Type boundary is set by the browser.
   * Setting it by hand omits the boundary and the server cannot parse the body.
   */
  async upload(file: File, name: string, signal?: AbortSignal): Promise<Dataset> {
    const form = new FormData();
    form.append("file", file);
    if (name) form.append("name", name);

    const response = await fetch(`${BASE}/v1/datasets`, {
      method: "POST",
      body: form,
      signal,
    });
    if (!response.ok) {
      throw new ApiError(response.status, await readErrorBody(response));
    }
    return (await response.json()) as Dataset;
  },

  cases(id: string, after = 0, signal?: AbortSignal): Promise<Page<DatasetCase>> {
    return request<Page<DatasetCase>>(`/v1/datasets/${id}/cases?after=${after}&limit=1000`, {
      signal,
    });
  },

  remove(id: string): Promise<void> {
    return request<void>(`/v1/datasets/${id}`, { method: "DELETE" });
  },
};

export const batchesApi = {
  list(signal?: AbortSignal): Promise<Page<Batch>> {
    return request<Page<Batch>>("/v1/batches", { signal });
  },

  get(id: string, signal?: AbortSignal): Promise<BatchDetail> {
    return request<BatchDetail>(`/v1/batches/${id}`, { signal });
  },

  create(
    body: {
      dataset_id: string;
      name: string;
      prompt_template: string;
      models: string[];
      tools?: string[];
      max_tokens?: number;
    },
    signal?: AbortSignal,
  ): Promise<BatchDetail> {
    return request<BatchDetail>("/v1/batches", {
      method: "POST",
      body,
      signal,
      withProviderKey: true,
    });
  },

  cancel(id: string): Promise<BatchDetail> {
    return request<BatchDetail>(`/v1/batches/${id}/cancel`, { method: "POST" });
  },
};

export const evalsApi = {
  score(
    body: { batch_id: string; scorer: string; config?: Record<string, unknown> },
    signal?: AbortSignal,
  ): Promise<ScoreBatchResponse> {
    // llm_judge creates judge runs, so scoring needs the key too.
    return request<ScoreBatchResponse>("/v1/evals/score", {
      method: "POST",
      body,
      signal,
      withProviderKey: true,
    });
  },

  /** Judging is asynchronous; the client polls this while judge runs drain. */
  collect(batchId: string): Promise<ScoreBatchResponse> {
    return request<ScoreBatchResponse>(`/v1/evals/collect/${batchId}`, { method: "POST" });
  },

  scores(batchId: string, scorer?: string, signal?: AbortSignal): Promise<Page<EvalScore>> {
    const query = new URLSearchParams({ batch_id: batchId, limit: "2000" });
    if (scorer) query.set("scorer", scorer);
    return request<Page<EvalScore>>(`/v1/evals/scores?${query}`, { signal });
  },
};
