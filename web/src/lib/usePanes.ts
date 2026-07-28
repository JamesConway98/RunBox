"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { track } from "./analytics";
import { DEFAULT_MAX_TOKENS, DEFAULT_MODEL, MAX_TOKENS_CEILING, MODELS } from "./models";

/**
 * Playground pane configuration, persisted across navigation and reload.
 *
 * Only the *configuration* is persisted, never the run ids. A restored pane
 * pointing at a run that finished three days ago would reconnect to a dead
 * stream and show a stale trace as though it were live. Config is what the
 * user arranged; results are what happened, and only one of those should
 * survive a reload.
 */

export interface PaneConfig {
  id: string;
  model: string;
  temperature: number | null;
  maxTokens: number;
  systemPrompt: string;
  tools: string[];
}

const STORAGE_KEY = "runbox-playground-panes-v1";
const MAX_PANES = 6;

let paneCounter = 0;

function nextPaneId(): string {
  paneCounter += 1;
  // Not crypto.randomUUID: this id has to be stable enough to be a React key
  // and readable enough to be useful in a bug report.
  return `pane-${Date.now().toString(36)}-${paneCounter}`;
}

export function createPane(overrides: Partial<PaneConfig> = {}): PaneConfig {
  return {
    id: nextPaneId(),
    model: DEFAULT_MODEL,
    temperature: null,
    maxTokens: DEFAULT_MAX_TOKENS,
    systemPrompt: "",
    tools: ["http_get"],
    ...overrides,
  };
}

function defaultPanes(): PaneConfig[] {
  // Two different models by default, because one pane does not communicate
  // what this screen is for.
  return [
    createPane({ model: MODELS[0]?.id ?? DEFAULT_MODEL }),
    createPane({ model: MODELS[1]?.id ?? DEFAULT_MODEL }),
  ];
}

function isPaneConfig(value: unknown): value is PaneConfig {
  if (typeof value !== "object" || value === null) return false;
  const pane = value as Record<string, unknown>;
  return typeof pane.id === "string" && typeof pane.model === "string";
}

function load(): PaneConfig[] | null {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return null;

    // Validate rather than trust. localStorage survives deploys, so a shape
    // from three versions ago is a normal thing to encounter, not an attack.
    const panes = parsed.filter(isPaneConfig).map((pane) => ({
      ...createPane(),
      ...pane,
      tools: Array.isArray(pane.tools) ? pane.tools.filter((t) => typeof t === "string") : [],
      // Panes saved before the default came down would otherwise keep a 20,000
      // ceiling forever, which is exactly the spend this change exists to stop.
      maxTokens: Math.min(
        typeof pane.maxTokens === "number" ? pane.maxTokens : DEFAULT_MAX_TOKENS,
        MAX_TOKENS_CEILING,
      ),
    }));
    return panes.length > 0 ? panes.slice(0, MAX_PANES) : null;
  } catch {
    return null;
  }
}

export function usePanes() {
  // Initialised empty and hydrated in an effect. Reading localStorage during
  // render would produce different markup on the server and the client, and
  // React would throw the whole tree away and re-render it.
  const [panes, setPanes] = useState<PaneConfig[]>([]);
  const [hydrated, setHydrated] = useState(false);
  const persistTimer = useRef<number | null>(null);

  useEffect(() => {
    setPanes(load() ?? defaultPanes());
    setHydrated(true);
  }, []);

  useEffect(() => {
    if (!hydrated) return;

    // Debounced: dragging a temperature slider would otherwise write to
    // localStorage on every pointer move, and localStorage writes are
    // synchronous and block the main thread.
    if (persistTimer.current !== null) {
      window.clearTimeout(persistTimer.current);
    }
    persistTimer.current = window.setTimeout(() => {
      try {
        localStorage.setItem(STORAGE_KEY, JSON.stringify(panes));
      } catch {
        // Quota or private browsing. Losing persistence is not worth an error.
      }
    }, 250);

    return () => {
      if (persistTimer.current !== null) window.clearTimeout(persistTimer.current);
    };
  }, [panes, hydrated]);

  const addPane = useCallback(() => {
    setPanes((current) => {
      if (current.length >= MAX_PANES) return current;
      track("pane_added", { pane_count: current.length + 1 });
      // Seed from the last pane so adding a fourth does not mean re-entering
      // the system prompt that the other three already share.
      const template = current[current.length - 1];
      return [
        ...current,
        createPane(
          template
            ? {
                model: template.model,
                systemPrompt: template.systemPrompt,
                tools: [...template.tools],
                maxTokens: template.maxTokens,
              }
            : {},
        ),
      ];
    });
  }, []);

  const removePane = useCallback((id: string) => {
    // Never drop to zero — an empty Playground has no affordance to recover.
    setPanes((current) => {
      if (current.length <= 1) return current;
      track("pane_removed", { pane_count: current.length - 1 });
      return current.filter((p) => p.id !== id);
    });
  }, []);

  const updatePane = useCallback((id: string, patch: Partial<PaneConfig>) => {
    if (patch.model) track("model_selected", { model: patch.model, surface: "playground" });
    setPanes((current) => current.map((p) => (p.id === id ? { ...p, ...patch } : p)));
  }, []);

  const movePane = useCallback((id: string, direction: -1 | 1) => {
    setPanes((current) => {
      const index = current.findIndex((p) => p.id === id);
      const target = index + direction;
      if (index < 0 || target < 0 || target >= current.length) return current;

      const next = current.slice();
      const [moved] = next.splice(index, 1);
      if (moved) next.splice(target, 0, moved);
      return next;
    });
  }, []);

  return {
    panes,
    hydrated,
    addPane,
    removePane,
    updatePane,
    movePane,
    canAdd: panes.length < MAX_PANES,
  };
}
