"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { ProviderKeyGate } from "@/components/ProviderKeyGate";
import { TraceTimeline } from "@/components/TraceTimeline";
import { Button } from "@/components/ui/button";
import { Badge, Card, CardBody, Skeleton } from "@/components/ui/primitives";
import { track } from "@/lib/analytics";
import { STATUS_TONE, formatCost, formatDuration } from "@/lib/models";
import type { RunStatus } from "@/lib/types";
import { useProviderKey } from "@/lib/useProviderKey";
import { useRunStream } from "@/lib/useRunStream";

interface Example {
  id: string;
  label: string;
  task: string;
  model: string;
}

interface DemoRun {
  id: string;
  status: RunStatus;
  task: string;
  model: string;
  result: string | null;
  duration_ms: number | null;
  cost_micros: number | null;
  created_at: string;
}

/**
 * The public demo. No signup, no API key.
 *
 * Everything here goes through /v1/demo/*, which resolves the demo tenant
 * server-side. There is no key in the client to find, and nothing a visitor
 * sends can name a different tenant.
 */
export default function DemoPage() {
  const [examples, setExamples] = useState<Example[]>([]);
  const [recent, setRecent] = useState<DemoRun[]>([]);
  const [runId, setRunId] = useState<string | null>(null);
  const [starting, setStarting] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const { key: providerKey, ready: keyReady } = useProviderKey();

  const loadRecent = useCallback(async () => {
    try {
      const response = await fetch("/api/v1/demo/runs?limit=8");
      if (response.ok) setRecent((await response.json()).data);
    } catch {
      // The examples still work without the history.
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void fetch("/api/v1/demo/examples")
      .then((r) => r.json())
      .then((body) => setExamples(body.examples))
      .catch(() => setExamples([]));
    void loadRecent();
  }, [loadRecent]);

  const start = useCallback(
    async (example: Example) => {
      setStarting(example.id);
      setNotice(null);
      try {
        const response = await fetch("/api/v1/demo/runs", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            ...(providerKey ? { "X-Provider-Key": providerKey } : {}),
          },
          body: JSON.stringify({ example_id: example.id }),
        });
        const body = await response.json();

        if (!response.ok) {
          // The 429 message names how long to wait, which is far better than a
          // generic failure on a page whose whole job is a good first
          // impression.
          setNotice(body.detail?.message ?? body.message ?? "Could not start the run.");
          return;
        }

        setRunId(body.id);
        track("run_started", { model: example.model, source: "demo", example: example.id });
      } catch {
        setNotice("Could not reach the API.");
      } finally {
        setStarting(null);
      }
    },
    [providerKey],
  );

  const demoStreamUrl = useCallback((id: string) => `/api/v1/demo/runs/${id}/stream`, []);
  const stream = useRunStream({
    runId,
    urlFor: demoStreamUrl,
    onFinal: () => void loadRecent(),
  });

  return (
    <div className="mx-auto max-w-4xl space-y-6">
      <header className="space-y-2">
        <h1 className="text-2xl font-semibold tracking-tight">Runbox, running</h1>
        <p className="text-muted">
          Pick an example. It runs in an isolated container with no network access, and
          the trace streams back here as it happens. No signup — bring your own model
          key and you can see exactly what it costs.
        </p>
      </header>

      {keyReady && !providerKey && <ProviderKeyGate />}

      <div className="grid gap-3 sm:grid-cols-3">
        {examples.length === 0
          ? Array.from({ length: 3 }, (_, i) => <Skeleton key={i} className="h-28 rounded-xl" />)
          : examples.map((example) => (
              <Card key={example.id} className="flex flex-col">
                <CardBody className="flex flex-1 flex-col gap-2">
                  <p className="text-sm font-medium">{example.label}</p>
                  <p className="flex-1 text-xs text-muted">{example.task}</p>
                  <Button
                    variant="primary"
                    size="sm"
                    onClick={() => void start(example)}
                    disabled={starting !== null || stream.isStreaming || !providerKey}
                  >
                    {starting === example.id ? "Starting…" : "Run this"}
                  </Button>
                </CardBody>
              </Card>
            ))}
      </div>

      {notice && (
        <p className="rounded-lg border border-warning/30 bg-warning/10 px-3 py-2 text-sm text-warning">
          {notice}
        </p>
      )}

      {runId && (
        <section className="space-y-2">
          <div className="flex items-center gap-2">
            <h2 className="text-sm font-medium">Live trace</h2>
            {stream.runStatus && (
              <Badge tone={STATUS_TONE[stream.runStatus]}>{stream.runStatus}</Badge>
            )}
            <span className="ml-auto font-mono text-xs text-subtle">
              {stream.tokenCount} tokens
              {stream.latencyToFirstTokenMs !== null &&
                ` · ${Math.round(stream.latencyToFirstTokenMs)}ms to first token`}
            </span>
          </div>
          <TraceTimeline
            segments={stream.segments}
            connection={stream.connection}
            isTerminal={stream.runStatus !== null}
          />
        </section>
      )}

      <section className="space-y-2">
        <h2 className="text-sm font-medium">Recent demo runs</h2>
        {loading ? (
          <Skeleton className="h-32 rounded-xl" />
        ) : recent.length === 0 ? (
          <Card className="px-6 py-8 text-center text-sm text-muted">
            Nothing yet. Start one above.
          </Card>
        ) : (
          <Card className="divide-y divide-border">
            {recent.map((run) => (
              <div key={run.id} className="flex flex-wrap items-center gap-3 px-3 py-2 text-xs">
                <Badge tone={STATUS_TONE[run.status]}>{run.status}</Badge>
                <span className="min-w-0 flex-1 truncate">{run.task}</span>
                <span className="font-mono text-subtle">{formatDuration(run.duration_ms)}</span>
                <span className="font-mono text-subtle">
                  {formatCost(run.cost_micros ?? 0)}
                </span>
              </div>
            ))}
          </Card>
        )}
      </section>

      <footer className="border-t border-border pt-4 text-sm text-muted">
        <p>
          The demo runs a fixed set of prompts and is rate limited by IP. For anything
          else,{" "}
          <Link href="/" className="text-accent hover:underline">
            open the Playground
          </Link>{" "}
          or{" "}
          <a
            href="https://github.com/JamesConway98/RunBox"
            className="text-accent hover:underline"
            target="_blank"
            rel="noreferrer"
          >
            read the source
          </a>
          .
        </p>
      </footer>
    </div>
  );
}
