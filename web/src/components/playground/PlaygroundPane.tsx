"use client";

import { memo, useEffect, useRef } from "react";

import { Button } from "@/components/ui/button";
import {
  Badge,
  Card,
  CardBody,
  CardHeader,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  Skeleton,
  X,
} from "@/components/ui/primitives";
import { cn } from "@/lib/cn";
import {
  DEFAULT_MAX_TOKENS,
  MAX_TOKENS_CEILING,
  MODELS,
  STATUS_TONE,
  formatCost,
  formatTokens,
  maxCostMicros,
} from "@/lib/models";
import type { TracePayload } from "@/lib/types";
import type { PaneConfig } from "@/lib/usePanes";
import { type Segment, useRunStream } from "@/lib/useRunStream";

export interface PaneRunState {
  runId: string | null;
  submitting: boolean;
  error: string | null;
}

interface Props {
  pane: PaneConfig;
  run: PaneRunState;
  index: number;
  paneCount: number;
  onUpdate: (patch: Partial<PaneConfig>) => void;
  onRemove: () => void;
  onMove: (direction: -1 | 1) => void;
  onCancel: () => void;
}

/**
 * One Playground pane, owning exactly one SSE stream.
 *
 * Each pane calls useRunStream independently, so N panes are N concurrent
 * EventSource connections appending tokens on their own schedules. That
 * independence is the point of the screen: nothing here coordinates, and one
 * pane being cancelled or erroring does not touch the others.
 *
 * memo'd because the parent re-renders whenever *any* pane's run state changes,
 * and re-rendering four streaming panes because a fifth got a token is exactly
 * the waste that makes this kind of screen feel bad.
 */
export const PlaygroundPane = memo(function PlaygroundPane({
  pane,
  run,
  index,
  paneCount,
  onUpdate,
  onRemove,
  onMove,
  onCancel,
}: Props) {
  const stream = useRunStream({ runId: run.runId });

  const inputTokens = stream.usage?.input_tokens ?? 0;
  const outputTokens = stream.usage?.output_tokens ?? 0;
  const cost = stream.usage?.cost_micros ?? 0;

  return (
    <Card className="flex min-h-80 flex-col">
      <CardHeader className="gap-2">
        <Select value={pane.model} onValueChange={(model) => onUpdate({ model })}>
          <SelectTrigger className="h-8 flex-1 border-0 bg-transparent px-1.5 text-sm font-medium
                                    hover:bg-raised">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {MODELS.map((m) => (
              <SelectItem key={m.id} value={m.id}>
                {m.name}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>

        {stream.runStatus ? (
          <Badge tone={STATUS_TONE[stream.runStatus]}>{stream.runStatus}</Badge>
        ) : stream.isStreaming ? (
          <Badge tone="accent">
            <span className="size-1.5 animate-pulse rounded-full bg-current" />
            streaming
          </Badge>
        ) : null}

        <div className="flex items-center">
          <Button
            variant="ghost"
            size="icon"
            className="size-7"
            onClick={() => onMove(-1)}
            disabled={index === 0}
            aria-label="Move pane left"
          >
            <span aria-hidden>←</span>
          </Button>
          <Button
            variant="ghost"
            size="icon"
            className="size-7"
            onClick={() => onMove(1)}
            disabled={index === paneCount - 1}
            aria-label="Move pane right"
          >
            <span aria-hidden>→</span>
          </Button>
          <Button
            variant="ghost"
            size="icon"
            className="size-7 text-subtle hover:text-danger"
            onClick={onRemove}
            disabled={paneCount <= 1}
            aria-label="Remove pane"
          >
            <X />
          </Button>
        </div>
      </CardHeader>

      <PaneParams pane={pane} onUpdate={onUpdate} disabled={stream.isStreaming} />

      <CardBody className="flex-1 overflow-hidden p-0">
        <Transcript
          segments={stream.segments}
          connection={stream.connection}
          submitting={run.submitting}
          error={run.error ?? stream.error}
          hasRun={Boolean(run.runId)}
        />
      </CardBody>

      <footer
        className="flex flex-wrap items-center gap-x-4 gap-y-1 border-t border-border px-3 py-1.5
                   text-[11px] text-subtle"
      >
        <Stat label="in" value={formatTokens(inputTokens)} />
        <Stat label="out" value={formatTokens(outputTokens)} />
        <Stat
          label="cost"
          value={formatCost(cost)}
          tone={cost > 0 ? "text-fg" : undefined}
        />
        {stream.latencyToFirstTokenMs !== null && (
          <Stat label="first token" value={`${Math.round(stream.latencyToFirstTokenMs)}ms`} />
        )}

        <span className="ml-auto flex items-center gap-3">
          {/* The ceiling, not a guess. Someone spending their own key should be
              able to see the worst case before they press Run. */}
          <span title={`This pane will not spend more than this on output`}>
            max {formatCost(maxCostMicros(pane.model, pane.maxTokens))}
          </span>
          {stream.isStreaming && (
            <Button variant="subtle" size="sm" className="h-6 px-2" onClick={onCancel}>
              Cancel
            </Button>
          )}
        </span>
      </footer>
    </Card>
  );
});

function Stat({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone?: string;
}) {
  return (
    <span className="flex items-baseline gap-1">
      <span className="text-subtle/70">{label}</span>
      <span className={cn("font-mono tabular-nums", tone)}>{value}</span>
    </span>
  );
}

function PaneParams({
  pane,
  onUpdate,
  disabled,
}: {
  pane: PaneConfig;
  onUpdate: (patch: Partial<PaneConfig>) => void;
  disabled: boolean;
}) {
  return (
    <div className="flex items-center gap-3 border-b border-border bg-surface/60 px-3 py-1.5
                    text-[11px]">
      <label className="flex items-center gap-1.5 text-muted">
        temp
        <input
          type="range"
          min={0}
          max={1}
          step={0.1}
          value={pane.temperature ?? 0.7}
          onChange={(e) => onUpdate({ temperature: Number(e.target.value) })}
          disabled={disabled}
          className="h-1 w-16 accent-accent disabled:opacity-40"
          aria-label="Temperature"
        />
        <span className="w-6 font-mono tabular-nums">{(pane.temperature ?? 0.7).toFixed(1)}</span>
      </label>

      <label className="flex items-center gap-1.5 text-muted">
        max
        <input
          type="number"
          min={64}
          max={MAX_TOKENS_CEILING}
          step={1000}
          value={pane.maxTokens}
          onChange={(e) => onUpdate({ maxTokens: Number(e.target.value) || DEFAULT_MAX_TOKENS })}
          disabled={disabled}
          className="w-16 rounded border border-border bg-bg px-1 py-0.5 font-mono
                     tabular-nums disabled:opacity-40"
          aria-label="Max tokens"
        />
      </label>
    </div>
  );
}

function Transcript({
  segments,
  connection,
  submitting,
  error,
  hasRun,
}: {
  segments: Segment[];
  connection: string;
  submitting: boolean;
  error: string | null;
  hasRun: boolean;
}) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const pinnedRef = useRef(true);

  // Follow the tail, but only while the reader is already at the bottom.
  // Yanking someone back down while they are reading an earlier tool result is
  // the single most irritating thing a live view can do.
  useEffect(() => {
    const el = scrollRef.current;
    if (!el || !pinnedRef.current) return;
    el.scrollTop = el.scrollHeight;
  }, [segments]);

  const onScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    // 24px of slack, so a pixel of rounding does not unpin the view.
    pinnedRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
  };

  if (error) {
    return (
      <div className="p-3">
        <p className="rounded-lg border border-danger/30 bg-danger/10 px-2.5 py-2 text-xs text-danger">
          {error}
        </p>
      </div>
    );
  }

  if (submitting || (hasRun && segments.length === 0 && connection !== "closed")) {
    return (
      <div className="space-y-2 p-3">
        <Skeleton className="h-3 w-2/3" />
        <Skeleton className="h-3 w-full" />
        <Skeleton className="h-3 w-4/5" />
        <Skeleton className="h-3 w-1/2" />
      </div>
    );
  }

  if (!hasRun) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-2 p-6 text-center">
        <div
          aria-hidden
          className="flex gap-1 text-subtle/50"
        >
          <span className="h-1 w-8 rounded-full bg-current" />
          <span className="h-1 w-5 rounded-full bg-current" />
          <span className="h-1 w-6 rounded-full bg-current" />
        </div>
        <p className="max-w-[22ch] text-xs leading-relaxed text-subtle">
          Output appears here as it streams.
        </p>
      </div>
    );
  }

  return (
    <div
      ref={scrollRef}
      onScroll={onScroll}
      className="h-full max-h-[24rem] space-y-1.5 overflow-y-auto scrollbar-thin p-3"
    >
      {segments.map((segment) => (
        <SegmentRow key={segment.id} segment={segment} />
      ))}
    </div>
  );
}

const SegmentRow = memo(function SegmentRow({ segment }: { segment: Segment }) {
  if (segment.kind === "text") {
    return (
      <p className="whitespace-pre-wrap text-sm leading-relaxed">{segment.text}</p>
    );
  }

  const payload = segment.event.payload as TracePayload;
  const base = "animate-fade-in font-mono text-[11px]";

  switch (payload.type) {
    case "llm_call":
      return <p className={cn(base, "text-subtle")}>→ {payload.model}</p>;
    case "tool_call":
      return (
        <p className={cn(base, "text-accent")}>
          ⚙ {payload.tool}({truncate(JSON.stringify(payload.args), 80)})
        </p>
      );
    case "tool_result":
      return (
        <p className={cn(base, payload.ok ? "text-success" : "text-danger")}>
          {payload.ok ? "✓" : "✗"} {payload.tool} · {payload.duration_ms}ms
        </p>
      );
    case "error":
      return <p className={cn(base, "text-danger")}>! {payload.message}</p>;
    case "final":
      return null; // the status badge in the header already says this
    default:
      return null;
  }
});

function truncate(value: string, max: number): string {
  return value.length <= max ? value : `${value.slice(0, max)}…`;
}
