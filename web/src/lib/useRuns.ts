"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError, api } from "@/lib/api";
import { type Run, isTerminal } from "@/lib/types";

interface Filters {
  status?: string;
  model?: string;
}

/**
 * The runs list: cursor-paginated, and live for anything still in flight.
 *
 * Polling rather than a websocket or an SSE channel per row. The list only
 * needs to know that a status changed, which is a handful of bytes every few
 * seconds; opening a stream per visible run to learn it would be strictly more
 * machinery for strictly less. The detail view is where streaming earns its
 * keep.
 *
 * Polling stops entirely once nothing on screen is in flight, so an idle tab
 * costs nothing.
 */
export function useRuns(filters: Filters = {}, pollMs = 3000) {
  const [runs, setRuns] = useState<Run[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const abortRef = useRef<AbortController | null>(null);
  const { status, model } = filters;

  const load = useCallback(
    async (opts: { silent?: boolean } = {}) => {
      abortRef.current?.abort();
      const controller = new AbortController();
      abortRef.current = controller;

      if (!opts.silent) setLoading(true);
      try {
        const page = await api.listRuns({ limit: 25, status, model }, controller.signal);
        setRuns(page.data);
        setCursor(page.next_cursor);
        setHasMore(page.has_more);
        setError(null);
      } catch (err) {
        if (controller.signal.aborted) return;
        setError(err instanceof ApiError ? err.message : "Could not load runs.");
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    },
    [status, model],
  );

  const loadMore = useCallback(async () => {
    if (!cursor || loadingMore) return;
    setLoadingMore(true);
    try {
      const page = await api.listRuns({ limit: 25, cursor, status, model });
      // Deduplicate on id. A run created between two page fetches shifts the
      // window, and keyset pagination makes that rare rather than impossible.
      setRuns((current) => {
        const seen = new Set(current.map((r) => r.id));
        return [...current, ...page.data.filter((r) => !seen.has(r.id))];
      });
      setCursor(page.next_cursor);
      setHasMore(page.has_more);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not load more runs.");
    } finally {
      setLoadingMore(false);
    }
  }, [cursor, loadingMore, status, model]);

  useEffect(() => {
    void load();
    return () => abortRef.current?.abort();
  }, [load]);

  // Refresh only while something is actually in flight.
  const hasActive = runs.some((run) => !isTerminal(run.status));
  useEffect(() => {
    if (!hasActive) return;
    const timer = window.setInterval(() => void load({ silent: true }), pollMs);
    return () => window.clearInterval(timer);
  }, [hasActive, load, pollMs]);

  return {
    runs,
    loading,
    loadingMore,
    error,
    hasMore,
    loadMore,
    refresh: () => load({ silent: true }),
  };
}
