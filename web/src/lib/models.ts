import type { RunStatus } from "./types";

export interface ModelInfo {
  id: string;
  name: string;
  provider: string;
  contextLength: number;
  inputMicrosPer1k: number;
  outputMicrosPer1k: number;
  supportsTools: boolean;
  blurb: string;
}

/**
 * The catalogue, mirrored from `model_pricing`.
 *
 * Static rather than fetched. It changes on the order of once a quarter, and a
 * network round trip before the Playground can render its first pane would make
 * the most important screen in the app feel slow for no benefit. The API
 * remains the authority — a run against an unknown model is rejected server
 * side, so a stale entry here produces a clear 400 rather than a wrong result.
 */
export const MODELS: ModelInfo[] = [
  {
    id: "claude-sonnet-5",
    name: "Claude Sonnet 5",
    provider: "anthropic",
    contextLength: 200_000,
    inputMicrosPer1k: 3_000,
    outputMicrosPer1k: 15_000,
    supportsTools: true,
    blurb: "The default. Strong reasoning at a sensible price.",
  },
  {
    id: "claude-haiku-4-5",
    name: "Claude Haiku 4.5",
    provider: "anthropic",
    contextLength: 200_000,
    inputMicrosPer1k: 800,
    outputMicrosPer1k: 4_000,
    supportsTools: true,
    blurb: "Fastest and cheapest. Good for high-volume batch work.",
  },
  {
    id: "claude-opus-5",
    name: "Claude Opus 5",
    provider: "anthropic",
    contextLength: 200_000,
    inputMicrosPer1k: 15_000,
    outputMicrosPer1k: 75_000,
    supportsTools: true,
    blurb: "Most capable, most expensive. Reach for it when the others miss.",
  },
  {
    id: "gpt-4o",
    name: "GPT-4o",
    provider: "openai",
    contextLength: 128_000,
    inputMicrosPer1k: 2_500,
    outputMicrosPer1k: 10_000,
    supportsTools: true,
    blurb: "Second provider, wired through the same interface.",
  },
  {
    id: "gpt-4o-mini",
    name: "GPT-4o mini",
    provider: "openai",
    contextLength: 128_000,
    inputMicrosPer1k: 150,
    outputMicrosPer1k: 600,
    supportsTools: true,
    blurb: "Cheapest option in the catalogue.",
  },
];

export const MODELS_BY_ID = new Map(MODELS.map((m) => [m.id, m]));

export const DEFAULT_MODEL = "claude-sonnet-5";

/**
 * Ceiling on output tokens for a new pane.
 *
 * Deliberately small. Runs execute on the visitor's own provider key, and
 * output is the expensive half — 20,000 tokens on Sonnet is about $0.30 for a
 * single run. 1,024 is a complete answer for anything you would type into a
 * playground and caps the worst case near a cent. Anyone who needs more can
 * raise it per pane.
 */
export const DEFAULT_MAX_TOKENS = 1024;

/** What a pane will let you set, short of the API's own 200k limit. */
export const MAX_TOKENS_CEILING = 32_000;

/**
 * The API's floor, mirrored.
 *
 * `CreateRunRequest.max_tokens` is `ge=64`, so anything below it is rejected
 * before a container is ever started. The number is duplicated here rather than
 * fetched because the alternative — finding out on submit — is what produced
 * "The request body or query parameters are invalid." for anyone who typed a
 * small number into a field whose `min` attribute the browser treats as
 * advisory.
 */
export const MIN_MAX_TOKENS = 64;

/** Bring a token ceiling inside what the API will accept. */
export function clampMaxTokens(value: number): number {
  if (!Number.isFinite(value)) return DEFAULT_MAX_TOKENS;
  return Math.min(Math.max(Math.round(value), MIN_MAX_TOKENS), MAX_TOKENS_CEILING);
}

/**
 * The most a run can cost, given its token ceiling.
 *
 * Input is unknown until the prompt is sent, so this prices output only and is
 * labelled as a ceiling rather than an estimate. Being wrong in the direction
 * of "cheaper than advertised" is the only acceptable direction here.
 */
export function maxCostMicros(modelId: string, maxTokens: number): number {
  const model = MODELS_BY_ID.get(modelId);
  if (!model) return 0;
  return Math.ceil((maxTokens * model.outputMicrosPer1k) / 1000);
}

export const AVAILABLE_TOOLS = [
  { id: "http_get", label: "http_get", blurb: "Fetch a URL through the allowlisting proxy" },
  { id: "read_file", label: "read_file", blurb: "Read a file from the run workspace" },
  { id: "list_files", label: "list_files", blurb: "List the run workspace" },
] as const;

/** Estimated cost in micros, matching the runner's integer arithmetic. */
export function estimateCostMicros(
  modelId: string,
  inputTokens: number,
  outputTokens: number,
): number {
  const model = MODELS_BY_ID.get(modelId);
  if (!model) return 0;
  return (
    Math.ceil((inputTokens * model.inputMicrosPer1k) / 1000) +
    Math.ceil((outputTokens * model.outputMicrosPer1k) / 1000)
  );
}

/**
 * Format micros as a currency string.
 *
 * Four decimal places because a single cheap run costs a fraction of a cent,
 * and "$0.00" next to a running trace reads as broken rather than as cheap.
 */
export function formatCost(micros: number): string {
  const dollars = micros / 1_000_000;
  if (dollars === 0) return "$0.0000";
  if (dollars < 0.0001) return "<$0.0001";
  return `$${dollars.toFixed(4)}`;
}

export function formatTokens(n: number): string {
  if (n < 1000) return String(n);
  if (n < 1_000_000) return `${(n / 1000).toFixed(1)}k`;
  return `${(n / 1_000_000).toFixed(2)}M`;
}

export function formatDuration(ms: number | null | undefined): string {
  if (ms == null) return "—";
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
  const minutes = Math.floor(ms / 60_000);
  const seconds = Math.round((ms % 60_000) / 1000);
  return `${minutes}m ${seconds}s`;
}

export const STATUS_TONE: Record<RunStatus, "neutral" | "accent" | "success" | "warning" | "danger"> =
  {
    queued: "neutral",
    running: "accent",
    succeeded: "success",
    failed: "danger",
    cancelled: "warning",
    timeout: "warning",
  };
