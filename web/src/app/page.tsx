"use client";

import { useCallback, useState } from "react";

import { ApiError, api } from "@/lib/api";
import type { TracePayload } from "@/lib/types";
import { type Segment, useRunStream } from "@/lib/useRunStream";

export default function Home() {
  const [task, setTask] = useState("What are the three most recent releases of Go?");
  const [runId, setRunId] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);

  const stream = useRunStream({ runId });

  const submit = useCallback(async () => {
    if (!task.trim() || submitting) return;
    setSubmitting(true);
    setSubmitError(null);
    setRunId(null);
    try {
      const created = await api.createRun({
        task: task.trim(),
        model: "claude-sonnet-5",
        tools: ["http_get"],
      });
      setRunId(created.id);
    } catch (err) {
      setSubmitError(
        err instanceof ApiError ? err.message : "Could not reach the API. Is it running?",
      );
    } finally {
      setSubmitting(false);
    }
  }, [task, submitting]);

  const cancel = useCallback(async () => {
    if (!runId) return;
    try {
      await api.cancelRun(runId);
    } catch {
      // The stream will report the terminal state either way.
    }
  }, [runId]);

  return (
    <main className="mx-auto max-w-3xl px-6 py-16">
      <header className="mb-10">
        <h1 className="text-2xl font-semibold tracking-tight">Runbox</h1>
        <p className="mt-1 text-sm text-muted">
          Submit a task. It runs in a sandboxed container and streams back here.
        </p>
      </header>

      <div className="space-y-3">
        <textarea
          value={task}
          onChange={(event) => setTask(event.target.value)}
          onKeyDown={(event) => {
            // Enter is a newline in a textarea, which is correct. Cmd-Enter
            // submits, which is what anyone who uses this daily will reach for.
            if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
              event.preventDefault();
              void submit();
            }
          }}
          rows={3}
          placeholder="Describe a task…"
          className="w-full resize-y rounded-lg border border-border bg-surface px-3 py-2
                     text-sm placeholder:text-subtle focus:border-accent"
        />

        <div className="flex items-center gap-3">
          <button
            onClick={() => void submit()}
            disabled={submitting || !task.trim() || stream.isStreaming}
            className="rounded-lg bg-accent px-4 py-2 text-sm font-medium text-accent-fg
                       transition-opacity hover:opacity-90 disabled:opacity-40"
          >
            {submitting ? "Starting…" : "Run"}
          </button>

          {stream.isStreaming && runId && (
            <button
              onClick={() => void cancel()}
              className="rounded-lg border border-border px-4 py-2 text-sm
                         transition-colors hover:bg-raised"
            >
              Cancel
            </button>
          )}

          <span className="ml-auto font-mono text-xs text-subtle">
            {runId ? `${stream.connection} · seq ${stream.lastSeq}` : "no run"}
          </span>
        </div>

        {submitError && (
          <p className="rounded-lg border border-danger/30 bg-danger/10 px-3 py-2 text-sm text-danger">
            {submitError}
          </p>
        )}
      </div>

      <section className="mt-10">
        {!runId ? (
          <EmptyState />
        ) : (
          <ol className="space-y-2">
            {stream.segments.map((segment) => (
              <TraceRow key={segment.id} segment={segment} />
            ))}
            {stream.segments.length === 0 && (
              <li className="text-sm text-muted">Waiting for the container to start…</li>
            )}
          </ol>
        )}

        {stream.runStatus && (
          <footer className="mt-6 flex flex-wrap gap-x-6 gap-y-1 border-t border-border pt-4
                             font-mono text-xs text-muted">
            <span>status {stream.runStatus}</span>
            <span>{stream.tokenCount} tokens</span>
            {stream.latencyToFirstTokenMs !== null && (
              <span>first token {Math.round(stream.latencyToFirstTokenMs)}ms</span>
            )}
          </footer>
        )}
      </section>
    </main>
  );
}

function EmptyState() {
  return (
    <div className="rounded-lg border border-dashed border-border px-6 py-10 text-center">
      <p className="text-sm font-medium">No run yet</p>
      <p className="mt-1 text-sm text-muted">
        Describe a task above and press Run. The trace appears here as it happens.
      </p>
    </div>
  );
}

function TraceRow({ segment }: { segment: Segment }) {
  if (segment.kind === "text") {
    return (
      <li className="animate-fade-in whitespace-pre-wrap text-sm leading-relaxed">
        {segment.text}
      </li>
    );
  }

  const payload = segment.event.payload as TracePayload;

  switch (payload.type) {
    case "llm_call":
      return (
        <Row seq={segment.seq} tone="muted">
          calling {payload.model}
        </Row>
      );
    case "tool_call":
      return (
        <Row seq={segment.seq} tone="accent">
          → {payload.tool}({JSON.stringify(payload.args)})
        </Row>
      );
    case "tool_result":
      return (
        <Row seq={segment.seq} tone={payload.ok ? "success" : "danger"}>
          ← {payload.tool} {payload.ok ? "ok" : "failed"} in {payload.duration_ms}ms
        </Row>
      );
    case "error":
      return (
        <Row seq={segment.seq} tone="danger">
          {payload.message}
        </Row>
      );
    case "final":
      return (
        <Row seq={segment.seq} tone="muted">
          finished: {payload.status}
        </Row>
      );
    default:
      return null;
  }
}

function Row({
  seq,
  tone,
  children,
}: {
  seq: number;
  tone: "muted" | "accent" | "success" | "danger";
  children: React.ReactNode;
}) {
  const toneClass = {
    muted: "text-muted",
    accent: "text-accent",
    success: "text-success",
    danger: "text-danger",
  }[tone];

  return (
    <li className={`animate-fade-in font-mono text-xs ${toneClass}`}>
      <span className="mr-2 text-subtle tabular-nums">{String(seq).padStart(3, "0")}</span>
      {children}
    </li>
  );
}
