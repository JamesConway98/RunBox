"use client";

import { useCallback, useEffect, useMemo, useReducer, useRef } from "react";

import { api } from "./api";
import type { RunStatus, TraceEvent, TracePayload, Usage } from "./types";

/**
 * Subscribe to one run's live trace.
 *
 * Three decisions here are load-bearing and none of them are obvious:
 *
 * 1. **Tokens are collapsed into text segments, not stored as rows.** A long
 *    run emits thousands of `token` events. Rendering one node each turns the
 *    timeline into a list with 8,000 children that React has to diff on every
 *    frame. Collapsing consecutive tokens into a single text segment keeps the
 *    node count proportional to the number of *steps*, which is what a reader
 *    actually perceives.
 *
 * 2. **Updates are batched to an animation frame.** A fast stream produces
 *    tokens quicker than the display refreshes, and one `setState` per token
 *    means dozens of wasted renders per frame. Tokens accumulate in a ref and
 *    flush once per frame.
 *
 * 3. **`seq` is the only source of truth for ordering and deduplication.**
 *    Reconnects replay from a cursor, so the same event can legitimately arrive
 *    twice. Anything at or below the high-water mark is dropped.
 */

export type Segment =
  | { kind: "text"; id: string; text: string }
  | { kind: "event"; id: string; seq: number; event: TraceEvent };

export interface RunStreamState {
  segments: Segment[];
  /** Every non-token event, for callers that want the raw trace. */
  events: TraceEvent[];
  lastSeq: number;
  connection: "idle" | "connecting" | "open" | "closed" | "error";
  runStatus: RunStatus | null;
  result: string | null;
  usage: Partial<Usage> | null;
  error: string | null;
  /** Milliseconds from subscribe to the first token. The number people feel. */
  latencyToFirstTokenMs: number | null;
  tokenCount: number;
}

type Action =
  | { type: "connecting" }
  | { type: "open" }
  | { type: "closed" }
  | { type: "failed"; message: string }
  | { type: "reset" }
  | { type: "batch"; events: TraceEvent[]; now: number; startedAt: number };

const initialState: RunStreamState = {
  segments: [],
  events: [],
  lastSeq: 0,
  connection: "idle",
  runStatus: null,
  result: null,
  usage: null,
  error: null,
  latencyToFirstTokenMs: null,
  tokenCount: 0,
};

function reducer(state: RunStreamState, action: Action): RunStreamState {
  switch (action.type) {
    case "connecting":
      return { ...state, connection: "connecting", error: null };
    case "open":
      return { ...state, connection: "open", error: null };
    case "closed":
      return { ...state, connection: "closed" };
    case "failed":
      return { ...state, connection: "error", error: action.message };
    case "reset":
      return { ...initialState };
    case "batch":
      return applyBatch(state, action);
  }
}

function applyBatch(state: RunStreamState, action: Action & { type: "batch" }): RunStreamState {
  let { lastSeq, runStatus, result, usage, latencyToFirstTokenMs, tokenCount } = state;
  let segments = state.segments;
  let events = state.events;
  let mutatedSegments = false;
  let mutatedEvents = false;

  for (const event of action.events) {
    // A replayed event after reconnect is not an error, it is the protocol
    // working. Drop anything we have already seen.
    if (event.seq <= lastSeq) continue;
    lastSeq = event.seq;

    const payload = event.payload as TracePayload;

    if (payload.type === "token") {
      const text = typeof payload.text === "string" ? payload.text : "";
      if (!text) continue;

      if (latencyToFirstTokenMs === null) {
        latencyToFirstTokenMs = action.now - action.startedAt;
      }
      tokenCount += 1;

      if (!mutatedSegments) {
        segments = segments.slice();
        mutatedSegments = true;
      }
      const tail = segments[segments.length - 1];
      if (tail?.kind === "text") {
        // Replace rather than mutate: the array is copied, but the object must
        // change identity too or memoised children will not re-render.
        segments[segments.length - 1] = { ...tail, text: tail.text + text };
      } else {
        segments.push({ kind: "text", id: `text-${event.seq}`, text });
      }
      continue;
    }

    // Usage updates the running totals but is not a timeline row. It arrives
    // after every model turn, and a trace that shows "usage" between each step
    // is noisier for the reader without telling them anything the footer is
    // not already showing.
    if (payload.type === "usage") {
      usage = {
        ...usage,
        input_tokens: payload.input_tokens,
        output_tokens: payload.output_tokens,
        tool_calls: payload.tool_calls,
      };
      continue;
    }

    if (!mutatedSegments) {
      segments = segments.slice();
      mutatedSegments = true;
    }
    if (!mutatedEvents) {
      events = events.slice();
      mutatedEvents = true;
    }

    segments.push({ kind: "event", id: `event-${event.seq}`, seq: event.seq, event });
    events.push(event);

    if (payload.type === "final") {
      runStatus = payload.status ?? "succeeded";
      result = payload.result ?? null;
      usage = payload.usage ?? null;
    }
  }

  if (lastSeq === state.lastSeq) return state;

  return {
    ...state,
    segments,
    events,
    lastSeq,
    runStatus,
    result,
    usage,
    latencyToFirstTokenMs,
    tokenCount,
  };
}

const EVENT_TYPES = [
  "llm_call",
  "token",
  "tool_call",
  "tool_result",
  "usage",
  "error",
  "final",
] as const;

export interface UseRunStreamOptions {
  /** Pass null to hold off — creating panes before a run exists is normal. */
  runId: string | null;
  enabled?: boolean;
  onFinal?: (status: RunStatus, usage: Partial<Usage> | null) => void;
}

export function useRunStream({ runId, enabled = true, onFinal }: UseRunStreamOptions) {
  const [state, dispatch] = useReducer(reducer, initialState);

  const sourceRef = useRef<EventSource | null>(null);
  const pendingRef = useRef<TraceEvent[]>([]);
  const frameRef = useRef<number | null>(null);
  const startedAtRef = useRef<number>(0);
  const lastSeqRef = useRef(0);

  // Kept in a ref so the flush closure does not need to be rebuilt whenever the
  // caller passes a new inline function.
  const onFinalRef = useRef(onFinal);
  useEffect(() => {
    onFinalRef.current = onFinal;
  }, [onFinal]);

  const flush = useCallback(() => {
    frameRef.current = null;
    const batch = pendingRef.current;
    if (batch.length === 0) return;
    pendingRef.current = [];
    dispatch({
      type: "batch",
      events: batch,
      now: performance.now(),
      startedAt: startedAtRef.current,
    });
  }, []);

  const schedule = useCallback(
    (event: TraceEvent) => {
      if (event.seq <= lastSeqRef.current) return;
      lastSeqRef.current = event.seq;
      pendingRef.current.push(event);
      // One flush per frame. Anything faster is work the display throws away.
      if (frameRef.current === null) {
        frameRef.current = requestAnimationFrame(flush);
      }
    },
    [flush],
  );

  const disconnect = useCallback(() => {
    sourceRef.current?.close();
    sourceRef.current = null;
    if (frameRef.current !== null) {
      cancelAnimationFrame(frameRef.current);
      frameRef.current = null;
    }
    flush();
  }, [flush]);

  useEffect(() => {
    if (!runId || !enabled) return;

    dispatch({ type: "reset" });
    dispatch({ type: "connecting" });
    pendingRef.current = [];
    lastSeqRef.current = 0;
    startedAtRef.current = performance.now();

    // No `after`: the browser replays Last-Event-ID on its own reconnects, so
    // resumption is handled by the transport rather than by this hook.
    const source = new EventSource(api.streamUrl(runId));
    sourceRef.current = source;

    source.onopen = () => dispatch({ type: "open" });

    const handle = (raw: MessageEvent) => {
      let payload: unknown;
      try {
        payload = JSON.parse(raw.data as string);
      } catch {
        return; // a frame we cannot parse is one we should not act on
      }
      const seq = Number(raw.lastEventId);
      if (!Number.isFinite(seq)) return;

      const event: TraceEvent = {
        seq,
        type: raw.type as TraceEvent["type"],
        payload: payload as TracePayload,
      };
      schedule(event);

      if (event.type === "final") {
        // Close explicitly. Otherwise EventSource treats the server's clean
        // close as a dropped connection and reconnects to a finished run
        // forever.
        source.close();
        sourceRef.current = null;
        flush();
        dispatch({ type: "closed" });
        const final = event.payload as Extract<TracePayload, { type: "final" }>;
        onFinalRef.current?.(final.status ?? "succeeded", final.usage ?? null);
      }
    };

    for (const type of EVENT_TYPES) {
      source.addEventListener(type, handle as EventListener);
    }

    source.onerror = () => {
      // EventSource reconnects on its own; CONNECTING means it is already
      // trying. Only a CLOSED source is genuinely over.
      if (source.readyState === EventSource.CLOSED) {
        dispatch({ type: "failed", message: "Connection lost." });
      } else {
        dispatch({ type: "connecting" });
      }
    };

    return () => {
      for (const type of EVENT_TYPES) {
        source.removeEventListener(type, handle as EventListener);
      }
      source.close();
      sourceRef.current = null;
      if (frameRef.current !== null) {
        cancelAnimationFrame(frameRef.current);
        frameRef.current = null;
      }
    };
  }, [runId, enabled, schedule, flush]);

  const isStreaming = state.connection === "open" || state.connection === "connecting";

  return useMemo(
    () => ({ ...state, isStreaming, disconnect }),
    [state, isStreaming, disconnect],
  );
}
