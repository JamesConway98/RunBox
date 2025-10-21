"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { TraceTimeline } from "@/components/TraceTimeline";
import { Button } from "@/components/ui/button";
import { ArrowLeft, Badge, Card, CardBody, Skeleton } from "@/components/ui/primitives";
import { ApiError, api } from "@/lib/api";
import {
  MODELS_BY_ID,
  STATUS_TONE,
  formatCost,
  formatDuration,
  formatTokens,
} from "@/lib/models";
import { type Run, isTerminal } from "@/lib/types";
import { useRunStream } from "@/lib/useRunStream";

export default function RunDetailPage() {
  const params = useParams<{ id: string }>();
  const runId = params.id;

  const [run, setRun] = useState<Run | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const loadRun = useCallback(async () => {
    try {
      setRun(await api.getRun(runId));
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not load this run.");
    } finally {
      setLoading(false);
    }
  }, [runId]);

  useEffect(() => {
    void loadRun();
  }, [loadRun]);

  // Refetch when the stream reports a terminal event, so the header picks up
  // the persisted result and the final usage row rather than showing only what
  // the stream happened to carry.
  const onFinal = useCallback(() => {
    void loadRun();
  }, [loadRun]);

  const stream = useRunStream({ runId, onFinal });

  const cancel = useCallback(async () => {
    try {
      setRun(await api.cancelRun(runId));
    } catch {
      void loadRun();
    }
  }, [runId, loadRun]);

  if (loading) return <DetailSkeleton />;

  if (error || !run) {
    return (
      <Card className="px-6 py-12 text-center">
        <p className="text-sm font-medium">Run not found</p>
        <p className="mt-1 text-sm text-muted">{error ?? "It may belong to another tenant."}</p>
        <Button variant="secondary" size="sm" className="mt-4" asChild>
          <Link href="/runs">Back to runs</Link>
        </Button>
      </Card>
    );
  }

  // The live stream is ahead of the fetched row while a run is in flight.
  const status = stream.runStatus ?? run.status;
  const model = MODELS_BY_ID.get(run.model);
  const liveInput = stream.usage?.input_tokens ?? run.usage?.input_tokens ?? 0;
  const liveOutput = stream.usage?.output_tokens ?? run.usage?.output_tokens ?? 0;

  return (
    <div className="space-y-4 pb-16">
      <Link
        href="/runs"
        className="inline-flex items-center gap-1.5 text-sm text-muted hover:text-fg"
      >
        <ArrowLeft />
        Runs
      </Link>

      <header className="space-y-3">
        <div className="flex flex-wrap items-center gap-2">
          <Badge tone={STATUS_TONE[status]}>{status}</Badge>
          <span className="font-mono text-xs text-subtle">{run.id}</span>
          {!isTerminal(status) && (
            <Button variant="subtle" size="sm" className="ml-auto" onClick={() => void cancel()}>
              Cancel run
            </Button>
          )}
        </div>

        <h1 className="text-lg font-medium leading-snug">{run.task}</h1>

        <dl className="flex flex-wrap gap-x-6 gap-y-1 text-xs text-muted">
          <Meta label="Model" value={model?.name ?? run.model} />
          <Meta label="Tools" value={run.tools.length ? run.tools.join(", ") : "none"} />
          <Meta label="Duration" value={formatDuration(run.duration_ms)} />
          <Meta label="Started" value={new Date(run.created_at).toLocaleString()} />
        </dl>
      </header>

      {run.error && (
        <Card className="border-danger/30 bg-danger/5">
          <CardBody className="font-mono text-xs text-danger">{run.error}</CardBody>
        </Card>
      )}

      <TraceTimeline
        segments={stream.segments}
        connection={stream.connection}
        isTerminal={isTerminal(status)}
      />

      {run.result && (
        <Card>
          <CardBody className="space-y-2">
            <h2 className="text-xs font-medium uppercase tracking-wide text-muted">Result</h2>
            <p className="whitespace-pre-wrap text-sm leading-relaxed">{run.result}</p>
          </CardBody>
        </Card>
      )}

      {/* Sticky so the numbers stay visible while scrolling a long trace —
          watching the cost climb is half the point of the screen. */}
      <footer
        className="sticky bottom-0 -mx-4 flex flex-wrap items-center gap-x-6 gap-y-1
                   border-t border-border bg-bg/95 px-4 py-2.5 font-mono text-xs
                   text-muted backdrop-blur"
      >
        <span>{formatTokens(liveInput)} in</span>
        <span>{formatTokens(liveOutput)} out</span>
        <span>{stream.usage?.tool_calls ?? run.usage?.tool_calls ?? 0} tool calls</span>
        <span className="text-fg">
          {formatCost(
            run.usage?.cost_micros ?? estimateFromStream(run.model, liveInput, liveOutput),
          )}
        </span>
        {stream.latencyToFirstTokenMs !== null && (
          <span className="ml-auto">{Math.round(stream.latencyToFirstTokenMs)}ms to first token</span>
        )}
      </footer>
    </div>
  );
}

function estimateFromStream(model: string, input: number, output: number): number {
  const pricing = MODELS_BY_ID.get(model);
  if (!pricing) return 0;
  // Mirrors the runner's integer arithmetic so a live estimate and the final
  // stored cost agree rather than differing by a rounding step.
  return (
    Math.ceil((input * pricing.inputMicrosPer1k) / 1000) +
    Math.ceil((output * pricing.outputMicrosPer1k) / 1000)
  );
}

function Meta({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex gap-1.5">
      <dt className="text-subtle">{label}</dt>
      <dd className="text-fg">{value}</dd>
    </div>
  );
}

function DetailSkeleton() {
  return (
    <div className="space-y-4">
      <Skeleton className="h-4 w-16" />
      <Skeleton className="h-6 w-24" />
      <Skeleton className="h-6 w-2/3" />
      <Card>
        <CardBody className="space-y-3">
          {Array.from({ length: 5 }, (_, i) => (
            <Skeleton key={i} className="h-4" style={{ width: `${90 - i * 12}%` }} />
          ))}
        </CardBody>
      </Card>
    </div>
  );
}
