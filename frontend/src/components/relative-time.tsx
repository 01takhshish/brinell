"use client";

import { useSyncExternalStore } from "react";

import { formatAbsolute, formatRelative } from "@/lib/format";

const TICK_MS = 30_000;

/**
 * One clock for the whole page.
 *
 * A runs table renders a hundred of these, and giving each its own `setInterval` would mean a
 * hundred timers and a hundred independent re-renders to move the same minute hand. Subscribers
 * share a single interval, and `getSnapshot` returns a value that only changes on a tick — React
 * requires a cached snapshot, and returning a fresh `Date.now()` per call would loop forever.
 */
let tick = 0;
let timer: ReturnType<typeof setInterval> | null = null;
const listeners = new Set<() => void>();

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  if (timer === null) {
    tick = Date.now();
    timer = setInterval(() => {
      tick = Date.now();
      for (const l of listeners) l();
    }, TICK_MS);
  }
  return () => {
    listeners.delete(listener);
    if (listeners.size === 0 && timer !== null) {
      clearInterval(timer);
      timer = null;
    }
  };
}

const getSnapshot = () => tick;
/** 0 means "never rendered on a client", which is how the server render is recognised below. */
const getServerSnapshot = () => 0;

/**
 * Renders the absolute timestamp on the server and swaps to "3 minutes ago" once mounted.
 *
 * Computing "3 minutes ago" during the server render produces HTML that is already stale when it
 * arrives and that disagrees with what React renders on the client — a hydration error. The
 * absolute form stays in `title`, because "2 days ago" is the wrong unit when you are reading a
 * build log against a deploy time.
 */
export function RelativeTime({ iso, className = "" }: { iso: string | null | undefined; className?: string }) {
  const now = useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot);

  if (!iso) return <span className={className}>—</span>;
  return (
    <time dateTime={iso} title={formatAbsolute(iso)} className={className}>
      {now === 0 ? formatAbsolute(iso) : formatRelative(iso, now)}
    </time>
  );
}
