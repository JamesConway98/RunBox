"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

/**
 * Fixed-height row virtualisation.
 *
 * Hand-rolled rather than pulled from a library, because the requirement here
 * is narrow — uniform row heights, one vertical scroller — and the whole
 * implementation is the sixty lines below. A general virtualiser earns its
 * weight when rows are variable-height or the grid scrolls in two directions,
 * and neither is true of a results table.
 *
 * What it buys: a batch of 3,000 cases across 3 models is 9,000 rows. Rendering
 * those as DOM nodes is roughly 100MB of layout and a scroll that stutters on
 * every frame. Rendering ~30 keeps it flat regardless of how large the batch
 * gets.
 *
 * Deliberately not solved here: variable row heights, horizontal virtualisation
 * and sticky grouped headers. Each would roughly double this file, and none of
 * them is needed by the table this exists for.
 */

export interface VirtualRow<T> {
  index: number;
  item: T;
  /** Absolute offset from the top of the scrolling content. */
  top: number;
}

export interface UseVirtualRowsOptions {
  rowHeight: number;
  /**
   * Rows rendered beyond each edge of the viewport. Without overscan, a fast
   * flick outruns the scroll handler and shows blank space; with too much, the
   * saving disappears. Six is roughly one frame of fast scrolling.
   */
  overscan?: number;
}

export function useVirtualRows<T>(
  items: readonly T[],
  { rowHeight, overscan = 6 }: UseVirtualRowsOptions,
) {
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const [scrollTop, setScrollTop] = useState(0);
  const [viewportHeight, setViewportHeight] = useState(0);
  const frameRef = useRef<number | null>(null);

  // Scroll events fire far more often than the display refreshes. Reading
  // scrollTop in the handler and setting state per event means a layout read
  // and a render per event; coalescing to one per frame is the difference
  // between smooth and janky on a trackpad.
  const onScroll = useCallback(() => {
    if (frameRef.current !== null) return;
    frameRef.current = requestAnimationFrame(() => {
      frameRef.current = null;
      const el = scrollRef.current;
      if (el) setScrollTop(el.scrollTop);
    });
  }, []);

  // ResizeObserver rather than a window resize listener: the container can
  // change height without the window doing so — a sidebar opening, a filter
  // row wrapping to two lines.
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;

    setViewportHeight(el.clientHeight);
    const observer = new ResizeObserver(([entry]) => {
      if (entry) setViewportHeight(entry.contentRect.height);
    });
    observer.observe(el);

    return () => {
      observer.disconnect();
      if (frameRef.current !== null) cancelAnimationFrame(frameRef.current);
    };
  }, []);

  const totalHeight = items.length * rowHeight;

  const visible = useMemo<VirtualRow<T>[]>(() => {
    if (viewportHeight === 0) {
      // Before the first measurement, render a screenful rather than nothing.
      // Rendering nothing means the table flashes empty on mount, which reads
      // as a loading failure.
      const guess = Math.min(items.length, 20);
      return items.slice(0, guess).map((item, index) => ({
        index,
        item,
        top: index * rowHeight,
      }));
    }

    const first = Math.max(0, Math.floor(scrollTop / rowHeight) - overscan);
    const count = Math.ceil(viewportHeight / rowHeight) + overscan * 2;
    const last = Math.min(items.length, first + count);

    const rows: VirtualRow<T>[] = [];
    for (let index = first; index < last; index++) {
      const item = items[index];
      if (item !== undefined) {
        rows.push({ index, item, top: index * rowHeight });
      }
    }
    return rows;
  }, [items, scrollTop, viewportHeight, rowHeight, overscan]);

  const scrollToIndex = useCallback(
    (index: number) => {
      scrollRef.current?.scrollTo({ top: index * rowHeight, behavior: "smooth" });
    },
    [rowHeight],
  );

  return { scrollRef, onScroll, visible, totalHeight, scrollToIndex };
}
