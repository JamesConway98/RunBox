"use client";

import { useCallback, useMemo, useRef, useState } from "react";

import { type PaneRunState, PlaygroundPane } from "@/components/playground/PlaygroundPane";
import { Button } from "@/components/ui/button";
import { Plus, Skeleton, Textarea } from "@/components/ui/primitives";
import { ApiError, api } from "@/lib/api";
import { usePanes } from "@/lib/usePanes";

const EMPTY_RUN: PaneRunState = { runId: null, submitting: false, error: null };

export default function PlaygroundPage() {
  const { panes, hydrated, addPane, removePane, updatePane, movePane, canAdd } = usePanes();

  const [prompt, setPrompt] = useState("");
  const [runs, setRuns] = useState<Record<string, PaneRunState>>({});

  // One AbortController per pane. Cancelling a single pane must abort only its
  // own in-flight create request — a shared controller would take down every
  // pane's submission, which is precisely the bug this screen exists to prove
  // is not present.
  const controllers = useRef(new Map<string, AbortController>());

  const setRun = useCallback((paneId: string, patch: Partial<PaneRunState>) => {
    setRuns((current) => ({
      ...current,
      [paneId]: { ...(current[paneId] ?? EMPTY_RUN), ...patch },
    }));
  }, []);

  const runAll = useCallback(async () => {
    const task = prompt.trim();
    if (!task) return;

    // Fan out. Promise.allSettled rather than Promise.all: one pane failing to
    // create its run must not prevent the others from starting.
    await Promise.allSettled(
      panes.map(async (pane) => {
        controllers.current.get(pane.id)?.abort();
        const controller = new AbortController();
        controllers.current.set(pane.id, controller);

        setRun(pane.id, { submitting: true, error: null, runId: null });
        try {
          const created = await api.createRun(
            {
              task,
              model: pane.model,
              tools: pane.tools,
              system_prompt: pane.systemPrompt || null,
              temperature: pane.temperature,
              max_tokens: pane.maxTokens,
            },
            controller.signal,
          );
          setRun(pane.id, { runId: created.id, submitting: false });
        } catch (err) {
          if (controller.signal.aborted) {
            setRun(pane.id, { submitting: false });
            return;
          }
          setRun(pane.id, {
            submitting: false,
            error: err instanceof ApiError ? err.message : "Could not reach the API.",
          });
        } finally {
          controllers.current.delete(pane.id);
        }
      }),
    );
  }, [panes, prompt, setRun]);

  const cancelPane = useCallback(
    async (paneId: string) => {
      // Abort the create request if it is still in flight, then ask the server
      // to stop the run if one already exists. Both are needed: a pane can be
      // cancelled in either window.
      controllers.current.get(paneId)?.abort();

      const runId = runs[paneId]?.runId;
      if (!runId) return;
      try {
        await api.cancelRun(runId);
      } catch {
        // The stream reports the terminal state regardless.
      }
    },
    [runs],
  );

  const cancelAll = useCallback(async () => {
    await Promise.allSettled(panes.map((pane) => cancelPane(pane.id)));
  }, [panes, cancelPane]);

  const anyActive = useMemo(
    () => panes.some((pane) => runs[pane.id]?.submitting || runs[pane.id]?.runId),
    [panes, runs],
  );

  return (
    <div className="space-y-5">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">Playground</h1>
          <p className="text-sm text-muted">
            One prompt, every pane, streaming at once.
          </p>
        </div>

        <div className="flex items-center gap-2">
          <Button variant="secondary" size="sm" onClick={addPane} disabled={!canAdd}>
            <Plus />
            Add pane
          </Button>
          {anyActive && (
            <Button variant="subtle" size="sm" onClick={() => void cancelAll()}>
              Cancel all
            </Button>
          )}
        </div>
      </header>

      {/* Panes above the composer so that adding a pane does not push the input
          off-screen, and so the eye lands on the results first. */}
      {!hydrated ? (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          <Skeleton className="h-[26rem] rounded-xl" />
          <Skeleton className="h-[26rem] rounded-xl" />
        </div>
      ) : (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {panes.map((pane, index) => (
            <PlaygroundPane
              key={pane.id}
              pane={pane}
              index={index}
              paneCount={panes.length}
              run={runs[pane.id] ?? EMPTY_RUN}
              onUpdate={(patch) => updatePane(pane.id, patch)}
              onRemove={() => removePane(pane.id)}
              onMove={(direction) => movePane(pane.id, direction)}
              onCancel={() => void cancelPane(pane.id)}
            />
          ))}
        </div>
      )}

      <div className="sticky bottom-4 rounded-xl border border-border bg-surface/95 p-3 backdrop-blur">
        <Textarea
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
              e.preventDefault();
              void runAll();
            }
          }}
          rows={2}
          placeholder="Ask every pane the same thing…"
          className="border-0 bg-transparent focus:ring-0"
        />
        <div className="mt-2 flex items-center gap-2">
          <Button variant="primary" onClick={() => void runAll()} disabled={!prompt.trim()}>
            Run on {panes.length} {panes.length === 1 ? "pane" : "panes"}
          </Button>
          <kbd className="rounded border border-border px-1.5 py-0.5 font-mono text-[10px] text-subtle">
            ⌘↵
          </kbd>
        </div>
      </div>
    </div>
  );
}
