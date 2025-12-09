export interface Dataset {
  id: string;
  name: string;
  description: string | null;
  case_count: number;
  created_at: string;
}

export interface DatasetCase {
  id: number;
  idx: number;
  input: string;
  expected: string | null;
  metadata: Record<string, unknown>;
}

export interface BatchProgress {
  total: number;
  completed: number;
  failed: number;
  in_flight: number;
  cost_micros: number;
}

export interface Batch {
  id: string;
  name: string;
  dataset_id: string;
  models: string[];
  status: "running" | "completed" | "cancelled";
  total_runs: number;
  created_at: string;
  finished_at: string | null;
}

export interface BatchDetail extends Batch {
  progress: BatchProgress;
}

export interface EvalScore {
  run_id: string;
  model: string;
  scorer: string;
  passed: boolean;
  score: number;
  detail: string | null;
  judge_run_id: string | null;
}

export interface ScoreSummary {
  model: string;
  scorer: string;
  total: number;
  passed: number;
  pass_rate: number;
  avg_score: number;
  avg_latency_ms: number | null;
  cost_micros: number;
}

export interface ScoreBatchResponse {
  scored: number;
  skipped: number;
  judge_runs_queued: number;
  summary: ScoreSummary[];
}

export const SCORERS = [
  { id: "exact_match", label: "Exact match", needsExpected: true },
  { id: "contains", label: "Contains", needsExpected: false },
  { id: "regex", label: "Regex", needsExpected: false },
  { id: "latency", label: "Latency", needsExpected: false },
  { id: "llm_judge", label: "LLM judge", needsExpected: true },
] as const;
