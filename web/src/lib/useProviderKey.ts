"use client";

import { useCallback, useEffect, useState } from "react";

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
 */

const STORAGE_KEY = "runbox-provider-key";

export interface ProviderKeyState {
  key: string | null;
  /** True once localStorage has been read, so the UI can avoid a flash. */
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
  const [key, setKey] = useState<string | null>(null);
  const [ready, setReady] = useState(false);

  // Read in an effect, not during render: localStorage does not exist on the
  // server, and reading it during render would make the markup differ between
  // server and client.
  useEffect(() => {
    try {
      setKey(localStorage.getItem(STORAGE_KEY));
    } catch {
      // Private browsing. The key just will not persist.
    }
    setReady(true);
  }, []);

  const save = useCallback((value: string) => {
    const trimmed = value.trim();
    if (!trimmed) return;
    setKey(trimmed);
    try {
      localStorage.setItem(STORAGE_KEY, trimmed);
    } catch {
      // Still usable for this session.
    }
  }, []);

  const clear = useCallback(() => {
    setKey(null);
    try {
      localStorage.removeItem(STORAGE_KEY);
    } catch {
      // Nothing to do.
    }
  }, []);

  return { key, ready, save, clear, masked: key ? mask(key) : null };
}

/** Read the key outside React, for the API client. */
export function readProviderKey(): string | null {
  if (typeof window === "undefined") return null;
  try {
    return localStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}
