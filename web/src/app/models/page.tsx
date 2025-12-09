"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useCallback, useMemo } from "react";

import { Button } from "@/components/ui/button";
import { Badge, Card, CardBody, Input, Skeleton } from "@/components/ui/primitives";
import { cn } from "@/lib/cn";
import { MODELS, formatCost } from "@/lib/models";

type SortKey = "name" | "input" | "output" | "context";

export default function ModelsPage() {
  return (
    <Suspense fallback={<Skeleton className="h-64 rounded-xl" />}>
      <Catalogue />
    </Suspense>
  );
}

function Catalogue() {
  const router = useRouter();
  const params = useSearchParams();

  const query = params.get("q") ?? "";
  const provider = params.get("provider") ?? "";
  const sort = (params.get("sort") as SortKey) ?? "name";

  // Every piece of view state is a query parameter. A filtered, sorted
  // catalogue is then a URL someone can paste into Slack, and the back button
  // steps through the states rather than leaving the page.
  const setParam = useCallback(
    (key: string, value: string) => {
      const next = new URLSearchParams(params.toString());
      if (value) next.set(key, value);
      else next.delete(key);
      router.replace(next.size ? `/models?${next}` : "/models", { scroll: false });
    },
    [params, router],
  );

  const providers = useMemo(() => [...new Set(MODELS.map((m) => m.provider))].sort(), []);

  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase();
    const filtered = MODELS.filter((model) => {
      if (provider && model.provider !== provider) return false;
      if (!needle) return true;
      return (
        model.name.toLowerCase().includes(needle) ||
        model.id.toLowerCase().includes(needle) ||
        model.blurb.toLowerCase().includes(needle)
      );
    });

    return filtered.slice().sort((a, b) => {
      switch (sort) {
        case "input":
          return a.inputMicrosPer1k - b.inputMicrosPer1k;
        case "output":
          return a.outputMicrosPer1k - b.outputMicrosPer1k;
        case "context":
          return b.contextLength - a.contextLength;
        default:
          return a.name.localeCompare(b.name);
      }
    });
  }, [query, provider, sort]);

  return (
    <div className="space-y-5">
      <header>
        <h1 className="text-xl font-semibold tracking-tight">Models</h1>
        <p className="text-sm text-muted">
          Prices are per 1,000 tokens, matching what the runner meters.
        </p>
      </header>

      <div className="flex flex-wrap items-center gap-2">
        <Input
          value={query}
          onChange={(e) => setParam("q", e.target.value)}
          placeholder="Search models…"
          className="h-8 w-56 text-xs"
          aria-label="Search models"
        />

        <div className="flex gap-1">
          <FilterChip active={!provider} onClick={() => setParam("provider", "")}>
            All
          </FilterChip>
          {providers.map((p) => (
            <FilterChip
              key={p}
              active={provider === p}
              onClick={() => setParam("provider", p)}
            >
              {p}
            </FilterChip>
          ))}
        </div>

        <select
          value={sort}
          onChange={(e) => setParam("sort", e.target.value)}
          className="ml-auto h-8 rounded-lg border border-border bg-bg px-2 text-xs"
          aria-label="Sort models"
        >
          <option value="name">Name</option>
          <option value="input">Cheapest input</option>
          <option value="output">Cheapest output</option>
          <option value="context">Largest context</option>
        </select>
      </div>

      {visible.length === 0 ? (
        <Card className="px-6 py-10 text-center">
          <p className="text-sm font-medium">No models match</p>
          <p className="mt-1 text-sm text-muted">Try clearing the search or the provider filter.</p>
          <Button
            variant="secondary"
            size="sm"
            className="mt-4"
            onClick={() => router.replace("/models")}
          >
            Clear filters
          </Button>
        </Card>
      ) : (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {visible.map((model) => (
            <Card key={model.id}>
              <CardBody className="space-y-2">
                <div className="flex items-start justify-between gap-2">
                  <div>
                    <p className="font-medium">{model.name}</p>
                    <p className="font-mono text-[11px] text-subtle">{model.id}</p>
                  </div>
                  <Badge>{model.provider}</Badge>
                </div>

                <p className="text-xs text-muted">{model.blurb}</p>

                <dl className="grid grid-cols-3 gap-2 border-t border-border pt-2 text-[11px]">
                  <Stat label="in / 1k" value={formatCost(model.inputMicrosPer1k)} />
                  <Stat label="out / 1k" value={formatCost(model.outputMicrosPer1k)} />
                  <Stat label="context" value={`${(model.contextLength / 1000).toFixed(0)}k`} />
                </dl>

                <Button variant="secondary" size="sm" className="w-full" asChild>
                  {/* Seeds a Playground pane rather than just describing the
                      model — the catalogue is a place people decide from. */}
                  <a href={`/?model=${encodeURIComponent(model.id)}`}>Try in Playground</a>
                </Button>
              </CardBody>
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}

function FilterChip({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={cn(
        "rounded-md border px-2 py-1 text-xs transition-colors",
        active
          ? "border-accent bg-accent/10 text-accent"
          : "border-border text-muted hover:bg-raised",
      )}
    >
      {children}
    </button>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-subtle">{label}</dt>
      <dd className="font-mono tabular-nums">{value}</dd>
    </div>
  );
}
