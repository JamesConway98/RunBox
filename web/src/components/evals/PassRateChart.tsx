"use client";

import { Card, CardBody } from "@/components/ui/primitives";
import type { ScoreSummary } from "@/lib/batchTypes";
import { MODELS_BY_ID, formatCost, formatDuration } from "@/lib/models";

/**
 * Pass rate and cost per model.
 *
 * Plain SVG and CSS rather than a charting library. This is a grouped bar chart
 * with one series — recharts would be ~90KB of JavaScript to draw rectangles
 * whose widths are already a percentage. A chart library earns its place when
 * there are axes to scale, brushing, or tooltips over dense data; none of that
 * is true here.
 *
 * The bars are also a table underneath, so the numbers are readable by a screen
 * reader rather than being locked inside a picture.
 */
export function PassRateChart({ summary }: { summary: ScoreSummary[] }) {
  if (summary.length === 0) return null;

  const byScorer = new Map<string, ScoreSummary[]>();
  for (const row of summary) {
    const list = byScorer.get(row.scorer) ?? [];
    list.push(row);
    byScorer.set(row.scorer, list);
  }

  const maxCost = Math.max(...summary.map((s) => s.cost_micros), 1);

  return (
    <div className="space-y-3">
      {[...byScorer.entries()].map(([scorer, rows]) => (
        <Card key={scorer}>
          <CardBody className="space-y-3">
            <h3 className="text-sm font-medium">
              {scorer.replace(/_/g, " ")}
              <span className="ml-2 font-mono text-xs font-normal text-subtle">
                {rows.reduce((n, r) => n + r.total, 0).toLocaleString()} scored
              </span>
            </h3>

            <table className="w-full text-xs">
              <caption className="sr-only">
                Pass rate, latency and cost per model for the {scorer} scorer
              </caption>
              <thead className="sr-only">
                <tr>
                  <th>Model</th>
                  <th>Pass rate</th>
                  <th>Average latency</th>
                  <th>Cost</th>
                </tr>
              </thead>
              <tbody>
                {rows
                  .slice()
                  .sort((a, b) => b.pass_rate - a.pass_rate)
                  .map((row) => (
                    <tr key={row.model}>
                      <td className="w-32 py-1 pr-3 align-middle text-muted">
                        {MODELS_BY_ID.get(row.model)?.name ?? row.model}
                      </td>
                      <td className="py-1 align-middle">
                        <div className="flex items-center gap-2">
                          <div className="h-4 flex-1 overflow-hidden rounded bg-raised">
                            <div
                              className="h-full rounded bg-accent transition-[width] duration-500"
                              style={{ width: `${Math.round(row.pass_rate * 100)}%` }}
                            />
                          </div>
                          <span className="w-20 shrink-0 text-right font-mono tabular-nums">
                            {Math.round(row.pass_rate * 100)}%
                            <span className="text-subtle">
                              {" "}
                              {row.passed}/{row.total}
                            </span>
                          </span>
                        </div>
                      </td>
                      <td className="w-20 py-1 pl-3 text-right font-mono tabular-nums text-subtle">
                        {formatDuration(row.avg_latency_ms ? Math.round(row.avg_latency_ms) : null)}
                      </td>
                      <td className="w-24 py-1 pl-3 text-right">
                        <div className="flex items-center justify-end gap-1.5">
                          <div className="h-1 w-10 overflow-hidden rounded bg-raised">
                            <div
                              className="h-full rounded bg-warning"
                              style={{ width: `${(row.cost_micros / maxCost) * 100}%` }}
                            />
                          </div>
                          <span className="font-mono tabular-nums text-subtle">
                            {formatCost(row.cost_micros)}
                          </span>
                        </div>
                      </td>
                    </tr>
                  ))}
              </tbody>
            </table>
          </CardBody>
        </Card>
      ))}
    </div>
  );
}
