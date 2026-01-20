"use client";

/**
 * Product analytics.
 *
 * A thin wrapper over Amplitude rather than calling it directly from
 * components, for three reasons that all turned out to matter:
 *
 * 1. **Event names are a closed set.** A union type means a typo is a compile
 *    error rather than a second event that quietly splits a funnel in half.
 * 2. **It degrades to a no-op.** No API key — local development, the public
 *    demo, a fork — and every call becomes a function that returns. Nothing
 *    needs `if (analytics)` at the call site.
 * 3. **Nothing identifiable leaves the browser.** The redaction below is not a
 *    policy document, it is a function, so it cannot be forgotten.
 */

type EventName =
  | "run_started"
  | "run_cancelled"
  | "model_selected"
  | "pane_added"
  | "pane_removed"
  | "playground_fanout"
  | "dataset_uploaded"
  | "batch_launched"
  | "batch_cancelled"
  | "eval_scored"
  | "trace_step_expanded"
  | "results_filtered"
  | "theme_toggled";

type Props = Record<string, string | number | boolean | null | undefined>;

const API_KEY = process.env.NEXT_PUBLIC_AMPLITUDE_API_KEY ?? "";
const ENDPOINT = "https://api2.amplitude.com/2/httpapi";

/**
 * Keys that must never be sent, checked by name.
 *
 * Prompts and outputs are the whole reason: knowing that someone ran a batch is
 * useful, knowing what they asked is theirs. `task`, `prompt`, `result` and
 * friends are dropped at the boundary rather than by remembering not to pass
 * them.
 */
const REDACTED = new Set([
  "task",
  "prompt",
  "input",
  "result",
  "output",
  "system_prompt",
  "api_key",
  "email",
]);

let deviceId: string | null = null;
let queue: unknown[] = [];
let flushTimer: number | null = null;

function getDeviceId(): string {
  if (deviceId) return deviceId;
  try {
    const stored = localStorage.getItem("runbox-device-id");
    if (stored) {
      deviceId = stored;
      return stored;
    }
    // Random, not derived from anything about the person. It exists to stitch
    // one browser's events into a session, and for nothing else.
    const fresh = crypto.randomUUID();
    localStorage.setItem("runbox-device-id", fresh);
    deviceId = fresh;
    return fresh;
  } catch {
    // Private browsing. A per-load id still makes a session coherent.
    deviceId = crypto.randomUUID();
    return deviceId;
  }
}

function sanitise(props: Props): Props {
  const clean: Props = {};
  for (const [key, value] of Object.entries(props)) {
    if (REDACTED.has(key)) continue;
    if (typeof value === "string" && value.length > 200) continue;
    clean[key] = value;
  }
  return clean;
}

/**
 * Record an event.
 *
 * Never throws and never awaits. An analytics failure must not be able to break
 * a feature, and a user should never wait on a metrics round trip.
 */
export function track(name: EventName, props: Props = {}): void {
  if (!API_KEY || typeof window === "undefined") return;

  queue.push({
    event_type: name,
    device_id: getDeviceId(),
    time: Date.now(),
    event_properties: sanitise(props),
    platform: "web",
    app_version: process.env.NEXT_PUBLIC_APP_VERSION ?? "dev",
  });

  // Batched. The Playground fires several events within a few hundred
  // milliseconds of one prompt, and one request beats five.
  if (flushTimer === null) {
    flushTimer = window.setTimeout(flush, 2000);
  }
  if (queue.length >= 25) flush();
}

function flush(): void {
  if (flushTimer !== null) {
    window.clearTimeout(flushTimer);
    flushTimer = null;
  }
  if (queue.length === 0) return;

  const events = queue;
  queue = [];

  const body = JSON.stringify({ api_key: API_KEY, events });

  // sendBeacon survives the page being closed, which is exactly when the last
  // batch of a session would otherwise be lost.
  if (navigator.sendBeacon?.(ENDPOINT, new Blob([body], { type: "application/json" }))) {
    return;
  }

  void fetch(ENDPOINT, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body,
    keepalive: true,
  }).catch(() => {
    // Swallowed on purpose. Losing a metric is not worth a console error in a
    // user's browser.
  });
}

/** Flush on page hide, so the final batch of a session is not dropped. */
export function initAnalytics(): void {
  if (!API_KEY || typeof window === "undefined") return;
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") flush();
  });
}
