// The wire format, mirrored from the control plane's Pydantic schemas.
//
// Hand-written rather than generated. The API surface is small enough that a
// generator would be more machinery than it saves, and these types are read far
// more often than they change.

export type RunStatus =
  | "queued"
  | "running"
  | "succeeded"
  | "failed"
  | "cancelled"
  | "timeout";

export const TERMINAL_STATUSES: readonly RunStatus[] = [
  "succeeded",
  "failed",
  "cancelled",
  "timeout",
] as const;

export function isTerminal(status: RunStatus): boolean {
  return TERMINAL_STATUSES.includes(status);
}

export type TraceEventType =
  | "llm_call"
  | "token"
  | "tool_call"
  | "tool_result"
  | "error"
  | "final";

export interface Usage {
  input_tokens: number;
  output_tokens: number;
  tool_calls: number;
  compute_ms: number;
  cost_micros: number;
}

export interface Run {
  id: string;
  status: RunStatus;
  task: string;
  model: string;
  tools: string[];
  result: string | null;
  error: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  duration_ms: number | null;
  usage: Usage | null;
}

export interface TraceEvent {
  seq: number;
  type: TraceEventType;
  payload: TracePayload;
  created_at?: string;
}

// A discriminated union so a switch over event.type narrows the payload.
// Anything the agent adds that the dashboard has not caught up with lands in
// the index signature rather than failing to parse.
export type TracePayload =
  | ({ type: "llm_call"; model: string; messages: number; tools: string[] } & Base)
  | ({ type: "token"; text: string } & Base)
  | ({ type: "tool_call"; tool: string; args: Record<string, unknown>; call_id: string } & Base)
  | ({
      type: "tool_result";
      tool: string;
      call_id: string;
      ok: boolean;
      output: string;
      duration_ms: number;
    } & Base)
  | ({ type: "error"; message: string; retryable?: boolean; source?: string } & Base)
  | ({ type: "final"; status: RunStatus; result: string | null; usage: Partial<Usage> } & Base);

interface Base {
  ts?: number;
  [key: string]: unknown;
}

export interface CreateRunRequest {
  task: string;
  model?: string;
  tools?: string[];
  system_prompt?: string | null;
  temperature?: number | null;
  timeout_s?: number;
  max_tokens?: number;
}

export interface RunCreated {
  id: string;
  status: RunStatus;
}

export interface Page<T> {
  data: T[];
  has_more: boolean;
  next_cursor: string | null;
}

export interface ApiErrorBody {
  error: string;
  message: string;
  detail?: Record<string, unknown> | null;
}
