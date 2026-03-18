"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";

import { PassRateChart } from "@/components/evals/PassRateChart";
import { ResultsTable, type ResultRow } from "@/components/evals/ResultsTable";
import { Button } from "@/components/ui/button";
import { ArrowLeft, Badge, Card, Skeleton } from "@/components/ui/primitives";
import { track } from "@/lib/analytics";
import { ApiError, api, batchesApi, evalsApi } from "@/lib/api";
import type { BatchDetail, EvalScore, ScoreSummary } from "@/lib/batchTypes";
import { SCORERS } from "@/lib/batchTypes";
import { formatCost } from "@/lib/models";
import type { Run } from "@/lib/types";

export default function BatchEvalPage() {
  const { id } = useParams<{ id: string }>();

  const [batch, setBatch] = useState<BatchDetail | null>(null);
  const [runs, setRuns] = useState<Run[]>([]);
  const [scores, setScores] = useState<EvalScore[]>([]);
  const [summary, setSummary] = useState<ScoreSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [scoring, setScoring] = useState<string | null>(null);

  const loadBatch = useCallback(async () => {
    try {
      setBatch(await batchesApi.get(id));
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not load this batch.");
    } finally {
      setLoading(false);
    }
  }, [id]);

  const loadScores = useCallback(async () => {
    try {
      setScores((await evalsApi.scores(id)).data);
    } catch {
      // Scores are supplementary; a failure here should not blank the page.
    }
  }, [id]);

  // Runs are fetched in pages until exhausted. A batch is bounded — it has
  // total_runs — so this terminates, and holding all of them client-side is
  // what lets the results table sort and filter without a round trip.
  const loadRuns = useCallback(async () => {
    const collected: Run[] = [];
    let cursor: string | null = null;
    for (let page = 0; page < 40; page++) {
      const result = await api.listRuns({ limit: 100, cursor: cursor ?? undefined });
      collected.push(...result.data);
      if (!result.has_more || !result.next_cursor) break;
      cursor = result.next_cursor;
    }
    setRuns(collected);
  }, []);

  useEffect(() => {
    void loadBatch();
    void loadScores();
    void loadRuns();
  }, [loadBatch, loadScores, loadRuns]);

  // Poll only while the batch is still producing work.
  const inFlight = batch?.progress.in_flight ?? 0;
  useEffect(() => {
    if (inFlight === 0) return;
    const timer = window.setInterval(() => {
      void loadBatch();
      void loadRuns();
    }, 3000);
    return () => window.clearInterval(timer);
  }, [inFlight, loadBatch, loadRuns]);

  const runScorer = useCallback(
    async (scorer: string) => {
      setScoring(scorer);
      try {
        const config =
          scorer === "latency" ? { threshold_ms: 10_000 } : scorer === "regex" ? {} : {};
        const result = await evalsApi.score({ batch_id: id, scorer, config });
        track("eval_scored", {
          scorer,
          scored: result.scored,
          judge_runs: result.judge_runs_queued,
          models: batch?.models.length ?? 0,
        });
        setSummary(result.summary);
        await loadScores();

        // llm_judge queues runs rather than scoring inline, so the verdicts
        // arrive later. Poll collect until they stop appearing.
        if (result.judge_runs_queued > 0) {
          for (let attempt = 0; attempt < 20; attempt++) {
            await new Promise((r) => setTimeout(r, 3000));
            const collected = await evalsApi.collect(id);
            setSummary(collected.summary);
            await loadScores();
            if (collected.scored === 0) break;
          }
        }
      } catch (err) {
        setError(err instanceof ApiError ? err.message : "Scoring failed.");
      } finally {
        setScoring(null);
      }
    },
    [id, loadScores, batch],
  );

  // The table's rows: one per run in this batch, joined to its score.
  const rows = useMemo<ResultRow[]>(() => {
    const scoreByRun = new Map(scores.map((s) => [s.run_id, s]));
    return runs
      .filter((run) => scoreByRun.has(run.id) || batch?.models.includes(run.model))
      .map((run) => ({
        runId: run.id,
        model: run.model,
        status: run.status,
        task: run.task,
        result: run.result,
        durationMs: run.duration_ms,
        costMicros: run.usage?.cost_micros ?? 0,
        score: scoreByRun.get(run.id) ?? null,
      }));
  }, [runs, scores, batch]);

  if (loading) return <Skeleton className="h-64 rounded-xl" />;

  if (!batch) {
    return (
      <Card className="px-6 py-12 text-center">
        <p className="text-sm font-medium">Batch not found</p>
        <p className="mt-1 text-sm text-muted">{error}</p>
        <Button variant="secondary" size="sm" className="mt-4" asChild>
          <Link href="/datasets">Back to datasets</Link>
        </Button>
      </Card>
    );
  }

  const { progress } = batch;
  const done = progress.completed + progress.failed;
  const percent = progress.total ? Math.round((done / progress.total) * 100) : 0;

  return (
    <div className="space-y-5">
      <Link
        href="/datasets"
        className="inline-flex items-center gap-1.5 text-sm text-muted hover:text-fg"
      >
        <ArrowLeft />
        Datasets
      </Link>

      <header className="space-y-3">
        <div className="flex flex-wrap items-center gap-2">
          <Badge tone={batch.status === "completed" ? "success" : "accent"}>{batch.status}</Badge>
          <h1 className="text-lg font-medium">{batch.name}</h1>
          {progress.in_flight > 0 && (
            <Button
              variant="subtle"
              size="sm"
              className="ml-auto"
              onClick={() => void batchesApi.cancel(id).then(loadBatch)}
            >
              Cancel batch
            </Button>
          )}
        </div>

        <div className="space-y-1.5">
          <div className="h-1.5 overflow-hidden rounded-full bg-raised">
            <div
              className="h-full rounded-full bg-accent transition-[width] duration-500"
              style={{ width: `${percent}%` }}
            />
          </div>
          <div className="flex flex-wrap gap-x-4 font-mono text-xs text-muted">
            <span>
              {done.toLocaleString()} / {progress.total.toLocaleString()}
            </span>
            <span className="text-success">{progress.completed.toLocaleString()} ok</span>
            {progress.failed > 0 && (
              <span className="text-danger">{progress.failed.toLocaleString()} failed</span>
            )}
            {progress.in_flight > 0 && <span>{progress.in_flight.toLocaleString()} in flight</span>}
            <span className="ml-auto">{formatCost(progress.cost_micros)} spent</span>
          </div>
        </div>
      </header>

      <section className="space-y-2">
        <h2 className="text-sm font-medium">Score</h2>
        <div className="flex flex-wrap gap-2">
          {SCORERS.map((scorer) => (
            <Button
              key={scorer.id}
              variant="secondary"
              size="sm"
              onClick={() => void runScorer(scorer.id)}
              disabled={scoring !== null || progress.completed === 0}
            >
              {scoring === scorer.id ? "Scoring…" : scorer.label}
            </Button>
          ))}
        </div>
        {progress.completed === 0 && (
          <p className="text-xs text-subtle">Nothing has succeeded yet — scoring needs output.</p>
        )}
      </section>

      {summary.length > 0 && <PassRateChart summary={summary} />}

      {error && (
        <p className="rounded-lg border border-danger/30 bg-danger/10 px-3 py-2 text-sm text-danger">
          {error}
        </p>
      )}

      <ResultsTable rows={rows} models={batch.models} />
    </div>
  );
}
