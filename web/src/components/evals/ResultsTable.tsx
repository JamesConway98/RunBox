"use client";

import { memo, useMemo, useState } from "react";

import { Button } from "@/components/ui/button";
import { Badge, Card, Input } from "@/components/ui/primitives";
import type { EvalScore } from "@/lib/batchTypes";
import { cn } from "@/lib/cn";
import { MODELS_BY_ID, STATUS_TONE, formatCost, formatDuration } from "@/lib/models";
import type { RunStatus } from "@/lib/types";
import { useVirtualRows } from "@/lib/useVirtualRows";

export interface ResultRow {
  runId: string;
  model: string;
  status: RunStatus;
  task: string;
  result: string | null;
  durationMs: number | null;
  costMicros: number;
  score: EvalScore | null;
}

// Fixed, and it must match the CSS. Virtualisation positions rows by
// arithmetic, so a row that renders taller than this overlaps its neighbour.
const ROW_HEIGHT = 36;

type SortKey = "index" | "model" | "duration" | "cost" | "score";

/**
 * The batch results table.
 *
 * Thousands of rows, no pagination, virtualised. Filtering and sorting happen
 * client-side over the whole set rather than server-side per page, which is the
 * reason there is no pagination to design around: the data is already here, so
 * a filter is a synchronous array operation rather than a round trip and a
 * spinner.
 */
export function ResultsTable({ rows, models }: { rows: ResultRow[]; models: string[] }) {
  const [query, setQuery] = useState("");
  const [modelFilter, setModelFilter] = useState<string>("");
  const [onlyFailures, setOnlyFailures] = useState(false);
  const [sort, setSort] = useState<{ key: SortKey; desc: boolean }>({
    key: "index",
    desc: false,
  });
  const [compare, setCompare] = useState<ResultRow | null>(null);

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();

    const result = rows.filter((row) => {
      if (modelFilter && row.model !== modelFilter) return false;
      if (onlyFailures && row.score?.passed !== false && row.status === "succeeded") return false;
      if (!needle) return true;
      return (
        row.task.toLowerCase().includes(needle) ||
        (row.result ?? "").toLowerCase().includes(needle)
      );
    });

    const direction = sort.desc ? -1 : 1;
    // Sorting a copy: mutating the memoised filter output would make the result
    // depend on how many times the component happened to render.
    return result.slice().sort((a, b) => {
      switch (sort.key) {
        case "model":
          return direction * a.model.localeCompare(b.model);
        case "duration":
          return direction * ((a.durationMs ?? 0) - (b.durationMs ?? 0));
        case "cost":
          return direction * (a.costMicros - b.costMicros);
        case "score":
          return direction * ((a.score?.score ?? -1) - (b.score?.score ?? -1));
        default:
          return 0;
      }
    });
  }, [rows, query, modelFilter, onlyFailures, sort]);

  const { scrollRef, onScroll, visible, totalHeight } = useVirtualRows(filtered, {
    rowHeight: ROW_HEIGHT,
  });

  const toggleSort = (key: SortKey) =>
    setSort((current) => ({ key, desc: current.key === key ? !current.desc : true }));

  return (
    <section className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="text-sm font-medium">Results</h2>
        <span className="font-mono text-xs text-subtle">
          {filtered.length.toLocaleString()}
          {filtered.length !== rows.length && ` of ${rows.length.toLocaleString()}`}
        </span>

        <div className="ml-auto flex flex-wrap items-center gap-2">
          <Input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Filter…"
            className="h-8 w-48 text-xs"
            aria-label="Filter results"
          />
          <select
            value={modelFilter}
            onChange={(e) => setModelFilter(e.target.value)}
            className="h-8 rounded-lg border border-border bg-bg px-2 text-xs"
            aria-label="Filter by model"
          >
            <option value="">All models</option>
            {models.map((m) => (
              <option key={m} value={m}>
                {MODELS_BY_ID.get(m)?.name ?? m}
              </option>
            ))}
          </select>
          <Button
            variant={onlyFailures ? "primary" : "secondary"}
            size="sm"
            onClick={() => setOnlyFailures((v) => !v)}
          >
            Failures only
          </Button>
        </div>
      </div>

      <Card className="overflow-hidden">
        <div
          className="grid items-center gap-2 border-b border-border px-3 py-2
                     text-xs font-medium text-muted"
          style={{ gridTemplateColumns: GRID }}
          role="row"
        >
          <span role="columnheader">#</span>
          <SortHeader label="Model" active={sort} k="model" onClick={toggleSort} />
          <span role="columnheader">Input</span>
          <span role="columnheader">Output</span>
          <SortHeader label="Score" active={sort} k="score" onClick={toggleSort} right />
          <SortHeader label="Time" active={sort} k="duration" onClick={toggleSort} right />
          <SortHeader label="Cost" active={sort} k="cost" onClick={toggleSort} right />
        </div>

        {filtered.length === 0 ? (
          <p className="px-3 py-10 text-center text-sm text-muted">
            {rows.length === 0 ? "No runs in this batch yet." : "Nothing matches these filters."}
          </p>
        ) : (
          <div
            ref={scrollRef}
            onScroll={onScroll}
            className="relative max-h-[32rem] overflow-y-auto scrollbar-thin"
            // The list is announced as such; individual rows are positioned
            // absolutely and would otherwise read as an unordered pile.
            role="grid"
            aria-rowcount={filtered.length}
          >
            {/* A spacer of the full height so the scrollbar reflects the real
                size of the data rather than the handful of rendered rows. */}
            <div style={{ height: totalHeight }} className="relative">
              {visible.map(({ index, item, top }) => (
                <Row
                  key={item.runId}
                  row={item}
                  index={index}
                  top={top}
                  onCompare={() => setCompare(item)}
                />
              ))}
            </div>
          </div>
        )}
      </Card>

      {compare && (
        <DiffPanel row={compare} rows={rows} onClose={() => setCompare(null)} />
      )}
    </section>
  );
}

const GRID = "3rem 8rem 1fr 1fr 5rem 4.5rem 5rem";

const Row = memo(function Row({
  row,
  index,
  top,
  onCompare,
}: {
  row: ResultRow;
  index: number;
  top: number;
  onCompare: () => void;
}) {
  return (
    <div
      role="row"
      aria-rowindex={index + 1}
      onClick={onCompare}
      className="absolute inset-x-0 grid cursor-pointer items-center gap-2 border-b
                 border-border px-3 text-xs hover:bg-raised/60"
      style={{ top, height: ROW_HEIGHT, gridTemplateColumns: GRID }}
    >
      <span className="font-mono tabular-nums text-subtle">{index + 1}</span>
      <span className="truncate text-muted">{MODELS_BY_ID.get(row.model)?.name ?? row.model}</span>
      <span className="truncate">{row.task}</span>
      <span className="truncate text-muted">{row.result ?? "—"}</span>
      <span className="text-right">
        {row.score ? (
          <Badge tone={row.score.passed ? "success" : "danger"}>
            {row.score.passed ? "pass" : "fail"}
          </Badge>
        ) : (
          <Badge tone={STATUS_TONE[row.status]}>{row.status}</Badge>
        )}
      </span>
      <span className="text-right font-mono tabular-nums text-subtle">
        {formatDuration(row.durationMs)}
      </span>
      <span className="text-right font-mono tabular-nums text-subtle">
        {formatCost(row.costMicros)}
      </span>
    </div>
  );
});

function SortHeader({
  label,
  active,
  k,
  onClick,
  right,
}: {
  label: string;
  active: { key: SortKey; desc: boolean };
  k: SortKey;
  onClick: (key: SortKey) => void;
  right?: boolean;
}) {
  const on = active.key === k;
  return (
    // aria-sort belongs on the columnheader, not on the control inside it.
    <span
      role="columnheader"
      aria-sort={on ? (active.desc ? "descending" : "ascending") : "none"}
      className={cn(right && "text-right")}
    >
      <button
        type="button"
        onClick={() => onClick(k)}
        className={cn("hover:text-fg", on && "text-fg")}
      >
        {label}
        {on && <span aria-hidden> {active.desc ? "↓" : "↑"}</span>}
      </button>
    </span>
  );
}

/**
 * Side-by-side comparison of every model's answer to one input.
 *
 * Selecting a row picks the *case*, not the run — comparing one model's output
 * to itself would be pointless, and the question anyone actually has when they
 * see a failure is "what did the others say?".
 */
function DiffPanel({
  row,
  rows,
  onClose,
}: {
  row: ResultRow;
  rows: ResultRow[];
  onClose: () => void;
}) {
  const siblings = rows.filter((r) => r.task === row.task);

  return (
    <Card>
      <div className="flex items-center gap-2 border-b border-border px-3 py-2">
        <h3 className="text-sm font-medium">Compare</h3>
        <span className="truncate font-mono text-xs text-subtle">{row.task}</span>
        <Button variant="ghost" size="sm" className="ml-auto" onClick={onClose}>
          Close
        </Button>
      </div>

      <div
        className="grid gap-3 p-3"
        style={{ gridTemplateColumns: `repeat(${Math.min(siblings.length, 3)}, minmax(0, 1fr))` }}
      >
        {siblings.map((sibling) => (
          <div key={sibling.runId} className="space-y-1.5">
            <div className="flex items-center gap-1.5">
              <span className="text-xs font-medium">
                {MODELS_BY_ID.get(sibling.model)?.name ?? sibling.model}
              </span>
              {sibling.score && (
                <Badge tone={sibling.score.passed ? "success" : "danger"}>
                  {sibling.score.passed ? "pass" : "fail"}
                </Badge>
              )}
            </div>
            <p className="whitespace-pre-wrap rounded-lg border border-border bg-raised/40 p-2 text-xs">
              {sibling.result ?? "(no output)"}
            </p>
            {sibling.score?.detail && (
              <p className="font-mono text-[11px] text-subtle">{sibling.score.detail}</p>
            )}
          </div>
        ))}
      </div>
    </Card>
  );
}
