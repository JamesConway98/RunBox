"use client";

import { useCallback, useEffect, useState } from "react";

import { Card, CardBody, Skeleton } from "@/components/ui/primitives";
import { MODELS_BY_ID, formatCost, formatTokens } from "@/lib/models";

interface UsageBucket {
  key: string;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  tool_calls: number;
  compute_ms: number;
  cost_micros: number;
  run_count: number;
}

interface UsageResponse {
  group_by: string;
  start: string;
  end: string;
  buckets: UsageBucket[];
  total: UsageBucket;
}

const RANGES = [
  { label: "7 days", days: 6 },
  { label: "30 days", days: 29 },
  { label: "90 days", days: 89 },
] as const;

export default function UsagePage() {
  const [days, setDays] = useState<number>(29);
  const [byDay, setByDay] = useState<UsageResponse | null>(null);
  const [byModel, setByModel] = useState<UsageResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    const end = new Date();
    const start = new Date(end.getTime() - days * 86_400_000);
    const range = `from=${iso(start)}&to=${iso(end)}`;

    try {
      const [dayResponse, modelResponse] = await Promise.all([
        fetch(`/api/v1/usage?${range}&group_by=day`),
        fetch(`/api/v1/usage?${range}&group_by=model`),
      ]);
      if (!dayResponse.ok || !modelResponse.ok) throw new Error("request failed");
      setByDay(await dayResponse.json());
      setByModel(await modelResponse.json());
      setError(null);
    } catch {
      setError("Could not load usage.");
    }
  }, [days]);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <div className="space-y-5">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">Usage</h1>
          <p className="text-sm text-muted">
            Tokens, compute and cost, rolled up from what the runner recorded.
          </p>
        </div>
        <div className="flex gap-1">
          {RANGES.map((range) => (
            <button
              key={range.days}
              type="button"
              onClick={() => setDays(range.days)}
              aria-pressed={days === range.days}
              className={`rounded-md border px-2 py-1 text-xs transition-colors ${
                days === range.days
                  ? "border-accent bg-accent/10 text-accent"
                  : "border-border text-muted hover:bg-raised"
              }`}
            >
              {range.label}
            </button>
          ))}
        </div>
      </header>

      {error && (
        <p className="rounded-lg border border-danger/30 bg-danger/10 px-3 py-2 text-sm text-danger">
          {error}
        </p>
      )}

      {!byDay ? (
        <Skeleton className="h-40 rounded-xl" />
      ) : (
        <>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <Tile label="Runs" value={byDay.total.run_count.toLocaleString()} />
            <Tile label="Tokens" value={formatTokens(byDay.total.total_tokens)} />
            <Tile label="Tool calls" value={byDay.total.tool_calls.toLocaleString()} />
            <Tile label="Cost" value={formatCost(byDay.total.cost_micros)} accent />
          </div>

          <DailyChart buckets={byDay.buckets} />

          {byModel && byModel.buckets.length > 0 && (
            <Card>
              <CardBody className="space-y-2">
                <h2 className="text-sm font-medium">By model</h2>
                <table className="w-full text-xs">
                  <thead>
                    <tr className="border-b border-border text-left text-muted">
                      <th className="py-1.5 font-medium">Model</th>
                      <th className="py-1.5 text-right font-medium">Runs</th>
                      <th className="py-1.5 text-right font-medium">Tokens</th>
                      <th className="py-1.5 text-right font-medium">Cost</th>
                    </tr>
                  </thead>
                  <tbody>
                    {byModel.buckets.map((bucket) => (
                      <tr key={bucket.key} className="border-b border-border last:border-0">
                        <td className="py-1.5">
                          {MODELS_BY_ID.get(bucket.key)?.name ?? bucket.key}
                        </td>
                        <td className="py-1.5 text-right font-mono tabular-nums text-muted">
                          {bucket.run_count.toLocaleString()}
                        </td>
                        <td className="py-1.5 text-right font-mono tabular-nums text-muted">
                          {formatTokens(bucket.total_tokens)}
                        </td>
                        <td className="py-1.5 text-right font-mono tabular-nums">
                          {formatCost(bucket.cost_micros)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </CardBody>
            </Card>
          )}
        </>
      )}
    </div>
  );
}

function DailyChart({ buckets }: { buckets: UsageBucket[] }) {
  if (buckets.length === 0) {
    return (
      <Card className="px-6 py-10 text-center">
        <p className="text-sm font-medium">No usage in this window</p>
        <p className="mt-1 text-sm text-muted">Start a run and it will appear here.</p>
      </Card>
    );
  }

  const max = Math.max(...buckets.map((b) => b.cost_micros), 1);

  return (
    <Card>
      <CardBody className="space-y-2">
        <h2 className="text-sm font-medium">Cost per day</h2>
        {/* Bars sized as a percentage of the maximum. There is no axis to
            scale and no interaction beyond a hover title, so a charting
            library would be pure weight. */}
        <div className="flex h-32 items-end gap-0.5">
          {buckets.map((bucket) => (
            <div
              key={bucket.key}
              title={`${bucket.key}: ${formatCost(bucket.cost_micros)} across ${bucket.run_count} runs`}
              className="flex-1 rounded-t bg-accent/70 transition-colors hover:bg-accent"
              style={{ height: `${Math.max(2, (bucket.cost_micros / max) * 100)}%` }}
            />
          ))}
        </div>
        <div className="flex justify-between font-mono text-[11px] text-subtle">
          <span>{buckets[0]?.key}</span>
          <span>{buckets[buckets.length - 1]?.key}</span>
        </div>
      </CardBody>
    </Card>
  );
}

function Tile({ label, value, accent }: { label: string; value: string; accent?: boolean }) {
  return (
    <Card>
      <CardBody>
        <p className="text-xs text-muted">{label}</p>
        <p
          className={`mt-0.5 font-mono text-xl tabular-nums ${accent ? "text-accent" : ""}`}
        >
          {value}
        </p>
      </CardBody>
    </Card>
  );
}

function iso(date: Date): string {
  return date.toISOString().slice(0, 10);
}
