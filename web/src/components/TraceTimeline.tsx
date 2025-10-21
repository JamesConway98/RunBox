"use client";

import { memo, useState } from "react";

import { Card, CardBody, Skeleton } from "@/components/ui/primitives";
import { cn } from "@/lib/cn";
import type { TracePayload } from "@/lib/types";
import type { Segment } from "@/lib/useRunStream";

/**
 * The trace as a vertical timeline.
 *
 * Steps are expandable rather than expanded. A tool result can be 24KB of page
 * text; rendering all of it inline turns the timeline into a wall and buries
 * the thing the reader came for, which is the *shape* of what the agent did.
 */
export function TraceTimeline({
  segments,
  connection,
  isTerminal,
}: {
  segments: Segment[];
  connection: string;
  isTerminal: boolean;
}) {
  if (segments.length === 0) {
    return isTerminal ? (
      <Card>
        <CardBody className="py-8 text-center text-sm text-muted">
          This run produced no trace events.
        </CardBody>
      </Card>
    ) : (
      <Card>
        <CardBody className="space-y-2">
          <Skeleton className="h-3 w-1/3" />
          <Skeleton className="h-3 w-2/3" />
          <Skeleton className="h-3 w-1/2" />
        </CardBody>
      </Card>
    );
  }

  return (
    <Card>
      <CardBody className="space-y-0">
        <ol className="relative space-y-0">
          {segments.map((segment) => (
            <TimelineRow key={segment.id} segment={segment} />
          ))}
        </ol>

        {connection === "open" && (
          <div className="flex items-center gap-2 pl-6 pt-3 text-xs text-subtle">
            <span className="size-1.5 animate-pulse rounded-full bg-accent" />
            streaming
          </div>
        )}
      </CardBody>
    </Card>
  );
}

const TimelineRow = memo(function TimelineRow({ segment }: { segment: Segment }) {
  if (segment.kind === "text") {
    return (
      <li className="relative py-1.5 pl-6">
        <Rail />
        <p className="whitespace-pre-wrap text-sm leading-relaxed">{segment.text}</p>
      </li>
    );
  }

  const payload = segment.event.payload as TracePayload;

  switch (payload.type) {
    case "llm_call":
      return (
        <Step tone="subtle" marker="○" label={`model call · ${payload.model}`}>
          <Detail label="messages" value={String(payload.messages)} />
          <Detail label="tools" value={payload.tools.join(", ") || "none"} />
        </Step>
      );

    case "tool_call":
      return (
        <Step tone="accent" marker="◆" label={`${payload.tool}()`} expandable>
          <pre className="overflow-x-auto text-xs">{JSON.stringify(payload.args, null, 2)}</pre>
        </Step>
      );

    case "tool_result":
      return (
        <Step
          tone={payload.ok ? "success" : "danger"}
          marker={payload.ok ? "✓" : "✗"}
          label={`${payload.tool} → ${payload.ok ? "ok" : "failed"} in ${payload.duration_ms}ms`}
          expandable
        >
          <pre className="max-h-80 overflow-auto whitespace-pre-wrap text-xs">
            {payload.output}
          </pre>
        </Step>
      );

    case "error":
      return (
        <Step tone="danger" marker="!" label={payload.message}>
          {payload.retryable && <Detail label="retryable" value="yes" />}
          {payload.source && <Detail label="source" value={payload.source} />}
        </Step>
      );

    case "final":
      return <Step tone="subtle" marker="●" label={`finished · ${payload.status}`} />;

    default:
      return null;
  }
});

const TONES = {
  subtle: "text-subtle",
  accent: "text-accent",
  success: "text-success",
  danger: "text-danger",
} as const;

function Step({
  tone,
  marker,
  label,
  expandable = false,
  children,
}: {
  tone: keyof typeof TONES;
  marker: string;
  label: string;
  expandable?: boolean;
  children?: React.ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const hasBody = Boolean(children);

  return (
    <li className="relative py-1.5 pl-6">
      <Rail />
      <span
        aria-hidden
        className={cn("absolute left-0 top-2 font-mono text-[10px]", TONES[tone])}
      >
        {marker}
      </span>

      {hasBody && expandable ? (
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
          className={cn(
            "text-left font-mono text-xs hover:underline",
            TONES[tone],
          )}
        >
          {label}
          <span className="ml-1.5 text-subtle">{open ? "▾" : "▸"}</span>
        </button>
      ) : (
        <p className={cn("font-mono text-xs", TONES[tone])}>{label}</p>
      )}

      {hasBody && (!expandable || open) && (
        <div className="mt-1.5 rounded-lg border border-border bg-raised/50 p-2">{children}</div>
      )}
    </li>
  );
}

function Detail({ label, value }: { label: string; value: string }) {
  return (
    <p className="font-mono text-[11px] text-muted">
      <span className="text-subtle">{label}</span> {value}
    </p>
  );
}

/** The connecting line. Decorative, so it is hidden from assistive tech. */
function Rail() {
  return (
    <span
      aria-hidden
      className="absolute left-[5px] top-0 h-full w-px bg-border"
    />
  );
}
