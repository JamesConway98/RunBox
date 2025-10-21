"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useCallback } from "react";

import { Button } from "@/components/ui/button";
import {
  Badge,
  Card,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  Skeleton,
} from "@/components/ui/primitives";
import {
  MODELS,
  MODELS_BY_ID,
  STATUS_TONE,
  formatCost,
  formatDuration,
  formatTokens,
} from "@/lib/models";
import type { Run } from "@/lib/types";
import { useRuns } from "@/lib/useRuns";

const STATUSES = ["queued", "running", "succeeded", "failed", "cancelled", "timeout"] as const;

export default function RunsPage() {
  // useSearchParams needs a Suspense boundary or the whole route opts out of
  // static rendering.
  return (
    <Suspense fallback={<RunsSkeleton />}>
      <RunsList />
    </Suspense>
  );
}

function RunsList() {
  const router = useRouter();
  const params = useSearchParams();

  const status = params.get("status") ?? "";
  const model = params.get("model") ?? "";

  // Filters live in the URL, not in component state, so a filtered view is a
  // link someone can send. It also means the back button undoes a filter,
  // which is what people expect it to do.
  const setFilter = useCallback(
    (key: string, value: string) => {
      const next = new URLSearchParams(params.toString());
      if (value) next.set(key, value);
      else next.delete(key);
      router.replace(next.size ? `/runs?${next}` : "/runs", { scroll: false });
    },
    [params, router],
  );

  const { runs, loading, loadingMore, error, hasMore, loadMore } = useRuns({
    status: status || undefined,
    model: model || undefined,
  });

  return (
    <div className="space-y-5">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">Runs</h1>
          <p className="text-sm text-muted">
            Every run for this tenant. In-flight rows update live.
          </p>
        </div>

        <div className="flex gap-2">
          <Select value={status || "all"} onValueChange={(v) => setFilter("status", v === "all" ? "" : v)}>
            <SelectTrigger className="h-8 w-36 text-xs">
              <SelectValue placeholder="All statuses" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All statuses</SelectItem>
              {STATUSES.map((s) => (
                <SelectItem key={s} value={s}>
                  {s}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>

          <Select value={model || "all"} onValueChange={(v) => setFilter("model", v === "all" ? "" : v)}>
            <SelectTrigger className="h-8 w-44 text-xs">
              <SelectValue placeholder="All models" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All models</SelectItem>
              {MODELS.map((m) => (
                <SelectItem key={m.id} value={m.id}>
                  {m.name}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      </header>

      {error && (
        <p className="rounded-lg border border-danger/30 bg-danger/10 px-3 py-2 text-sm text-danger">
          {error}
        </p>
      )}

      {loading ? (
        <RunsSkeleton />
      ) : runs.length === 0 ? (
        <EmptyRuns filtered={Boolean(status || model)} />
      ) : (
        <>
          <Card className="overflow-hidden">
            {/* A real table, not a grid of divs. Screen readers announce row and
                column position from the table semantics, and nothing here needs
                a layout a table cannot express. */}
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-border text-left text-xs text-muted">
                    <Th>Status</Th>
                    <Th className="min-w-64">Task</Th>
                    <Th>Model</Th>
                    <Th className="text-right">Duration</Th>
                    <Th className="text-right">Tokens</Th>
                    <Th className="text-right">Cost</Th>
                    <Th className="text-right">Created</Th>
                  </tr>
                </thead>
                <tbody>
                  {runs.map((run) => (
                    <RunRow key={run.id} run={run} />
                  ))}
                </tbody>
              </table>
            </div>
          </Card>

          {hasMore && (
            <div className="flex justify-center">
              <Button variant="secondary" onClick={() => void loadMore()} disabled={loadingMore}>
                {loadingMore ? "Loading…" : "Load more"}
              </Button>
            </div>
          )}
        </>
      )}
    </div>
  );
}

function Th({ children, className = "" }: { children: React.ReactNode; className?: string }) {
  return <th className={`px-3 py-2 font-medium ${className}`}>{children}</th>;
}

function RunRow({ run }: { run: Run }) {
  const model = MODELS_BY_ID.get(run.model);
  const tokens = run.usage ? run.usage.input_tokens + run.usage.output_tokens : 0;

  return (
    <tr className="border-b border-border last:border-0 hover:bg-raised/60">
      <td className="px-3 py-2">
        <Badge tone={STATUS_TONE[run.status]}>{run.status}</Badge>
      </td>
      <td className="max-w-md px-3 py-2">
        <Link href={`/runs/${run.id}`} className="line-clamp-1 hover:text-accent">
          {run.task}
        </Link>
      </td>
      <td className="whitespace-nowrap px-3 py-2 text-muted">{model?.name ?? run.model}</td>
      <td className="px-3 py-2 text-right font-mono text-xs tabular-nums text-muted">
        {formatDuration(run.duration_ms)}
      </td>
      <td className="px-3 py-2 text-right font-mono text-xs tabular-nums text-muted">
        {tokens ? formatTokens(tokens) : "—"}
      </td>
      <td className="px-3 py-2 text-right font-mono text-xs tabular-nums text-muted">
        {run.usage ? formatCost(run.usage.cost_micros) : "—"}
      </td>
      <td className="whitespace-nowrap px-3 py-2 text-right text-xs text-subtle">
        {relativeTime(run.created_at)}
      </td>
    </tr>
  );
}

function EmptyRuns({ filtered }: { filtered: boolean }) {
  return (
    <Card className="px-6 py-12 text-center">
      <p className="text-sm font-medium">{filtered ? "No matching runs" : "No runs yet"}</p>
      <p className="mx-auto mt-1 max-w-sm text-sm text-muted">
        {filtered
          ? "Nothing matches these filters. Try clearing one."
          : "Start one from the Playground, or POST to /v1/runs with your API key."}
      </p>
      {!filtered && (
        <Button variant="primary" size="sm" className="mt-4" asChild>
          <Link href="/">Open the Playground</Link>
        </Button>
      )}
    </Card>
  );
}

function RunsSkeleton() {
  return (
    <Card className="divide-y divide-border">
      {Array.from({ length: 6 }, (_, i) => (
        <div key={i} className="flex items-center gap-4 px-3 py-3">
          <Skeleton className="h-5 w-16" />
          <Skeleton className="h-4 flex-1" />
          <Skeleton className="h-4 w-24" />
          <Skeleton className="h-4 w-12" />
        </div>
      ))}
    </Card>
  );
}

function relativeTime(iso: string): string {
  const then = new Date(iso).getTime();
  const seconds = Math.round((Date.now() - then) / 1000);

  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86_400) return `${Math.floor(seconds / 3600)}h ago`;
  if (seconds < 604_800) return `${Math.floor(seconds / 86_400)}d ago`;
  return new Date(iso).toLocaleDateString();
}
