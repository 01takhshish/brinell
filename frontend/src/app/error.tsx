"use client";

import { useEffect } from "react";

/**
 * The most likely error in this app by a wide margin is "the API is not running", so the fallback
 * says that rather than apologising in the abstract. `digest` is the only handle on a server-side
 * error's real message — Next strips the message itself out of the client bundle on purpose — so
 * it is shown, because a support conversation without it is a guessing game.
 */
export default function ErrorBoundary({ error, reset }: { error: Error & { digest?: string }; reset: () => void }) {
  useEffect(() => {
    console.error(error);
  }, [error]);

  return (
    <main className="mx-auto max-w-lg px-4 py-20">
      <h1 className="text-lg font-semibold">Something went wrong</h1>
      <p className="mt-2 text-sm text-muted">
        Crucible could not load this page. If the API is not running, start it with{" "}
        <code className="font-mono text-xs">make dev</code> and try again.
      </p>
      <p className="wrap-anywhere mt-3 rounded border bg-panel px-2.5 py-1.5 font-mono text-xs text-muted">
        {error.message || "no message"}
        {error.digest && <span className="block text-faint">digest {error.digest}</span>}
      </p>
      <button
        type="button"
        onClick={reset}
        className="mt-4 rounded bg-text px-3 py-1.5 text-sm font-medium text-bg"
      >
        Try again
      </button>
    </main>
  );
}
