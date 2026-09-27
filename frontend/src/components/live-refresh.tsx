"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

/**
 * Re-renders the server component tree on an interval while a run is still live.
 *
 * `router.refresh()` rather than client-side polling of the API: the fetch stays on the server
 * where the session cookie already is, and the page keeps a single source of truth instead of
 * growing a second, client-side copy of the run that can drift from the rendered one.
 *
 * It stops the moment the run reaches a terminal status, so a finished run costs nothing. The
 * page also stops polling when the tab is hidden — a backgrounded dashboard left open overnight
 * would otherwise keep a worker-shaped load on the API for no one's benefit.
 */
export function LiveRefresh({ active, intervalMs = 4000 }: { active: boolean; intervalMs?: number }) {
  const router = useRouter();
  const [visible, setVisible] = useState(true);

  useEffect(() => {
    const onVisibility = () => setVisible(!document.hidden);
    onVisibility();
    document.addEventListener("visibilitychange", onVisibility);
    return () => document.removeEventListener("visibilitychange", onVisibility);
  }, []);

  useEffect(() => {
    if (!active || !visible) return;
    const timer = setInterval(() => router.refresh(), intervalMs);
    return () => clearInterval(timer);
  }, [active, visible, intervalMs, router]);

  if (!active) return null;
  return (
    <span className="text-xs text-faint" aria-live="polite">
      {visible ? "live · updating" : "paused"}
    </span>
  );
}
