"use client";

import { useCallback, useSyncExternalStore } from "react";

/**
 * The visitor's own model provider key.
 *
 * Held in localStorage and sent as a header on run creation. It is never posted
 * to Runbox for storage, never appears in a URL, and is never included in an
 * analytics event — `analytics.ts` drops `api_key` at the boundary, and this
 * hook deliberately does not name it anything else.
 *
 * localStorage rather than a cookie: a cookie would be attached to every
 * request to the origin automatically, including ones that have no business
 * seeing it. This way the key travels only on the calls that need it.
 *
 * The honest caveat, which the UI states too: localStorage is readable by any
 * script running on this origin. That is fine for a single-origin app with no
 * third-party scripts, and it is why there are none.
 *
 * ---
 *
 * This is an external store rather than `useState`, and that is the whole point
 * of the rewrite. With local state, every component calling this hook held its
 * own copy: saving a key in the card updated that card and nothing else, so the
 * header still read "no key set" and the Run button stayed disabled while the
 * key sat in localStorage. One value that several components read has to live
 * outside all of them.
 *
 * `useSyncExternalStore` also gets cross-tab updates for free via the storage
 * event, and gives a correct server snapshot so nothing renders a key on the
 * server that only exists in a browser.
 */

const STORAGE_KEY = "runbox-provider-key";

// Cached so getSnapshot returns a stable value. Reading localStorage on every
// call would be a fresh read each render, which is allowed but wasteful, and
// returning a new object would loop forever.
let cached: string | null = null;
let hydrated = false;

const listeners = new Set<() => void>();

function read(): string | null {
  try {
    return localStorage.getItem(STORAGE_KEY);
  } catch {
    // Private browsing, or storage disabled entirely.
    return null;
  }
}

function emit(): void {
  for (const listener of listeners) listener();
}

function subscribe(listener: () => void): () => void {
  if (!hydrated) {
    cached = read();
    hydrated = true;
  }
  listeners.add(listener);

  // Fires when another tab writes. Without it, setting a key in one tab leaves
  // every other tab convinced there is none.
  const onStorage = (event: StorageEvent) => {
    if (event.key !== null && event.key !== STORAGE_KEY) return;
    cached = read();
    emit();
  };
  window.addEventListener("storage", onStorage);

  return () => {
    listeners.delete(listener);
    window.removeEventListener("storage", onStorage);
  };
}

function getSnapshot(): string | null {
  if (!hydrated) {
    cached = read();
    hydrated = true;
  }
  return cached;
}

// There is no key on the server, and claiming otherwise would make the first
// client render disagree with the markup.
function getServerSnapshot(): string | null {
  return null;
}

export interface ProviderKeyState {
  key: string | null;
  /** True once the browser value has been read, so the UI can avoid a flash. */
  ready: boolean;
  save: (value: string) => void;
  clear: () => void;
  /** Safe to render: enough to recognise, not enough to use. */
  masked: string | null;
}

function mask(key: string): string {
  if (key.length <= 14) return "…";
  return `${key.slice(0, 11)}…${key.slice(-4)}`;
}

export function useProviderKey(): ProviderKeyState {
  const key = useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot);

  const save = useCallback((value: string) => {
    const trimmed = value.trim();
    if (!trimmed) return;
    try {
      localStorage.setItem(STORAGE_KEY, trimmed);
    } catch {
      // Still usable for this session even if it will not persist.
    }
    cached = trimmed;
    hydrated = true;
    emit();
  }, []);

  const clear = useCallback(() => {
    try {
      localStorage.removeItem(STORAGE_KEY);
    } catch {
      // Nothing to do.
    }
    cached = null;
    hydrated = true;
    emit();
  }, []);

  return {
    key,
    // Hydration is synchronous here, so there is no window in which the value
    // is unknown on the client.
    ready: typeof window !== "undefined",
    save,
    clear,
    masked: key ? mask(key) : null,
  };
}

/** Read the key outside React, for the API client. */
export function readProviderKey(): string | null {
  if (typeof window === "undefined") return null;
  return getSnapshot();
}
