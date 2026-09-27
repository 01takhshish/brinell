"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";

function LoginForm() {
  const router = useRouter();
  const next = useSearchParams().get("next") ?? "/";
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  async function onSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setPending(true);
    setError(null);
    const form = new FormData(event.currentTarget);
    try {
      const response = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: form.get("email"), password: form.get("password") }),
      });
      if (!response.ok) {
        const body = (await response.json().catch(() => ({}))) as { detail?: string };
        setError(body.detail ?? "Login failed");
        return;
      }
      // `refresh` before `push`: the layout above this route already rendered as signed-out, and
      // without it the cached server render would be reused and the redirect would bounce back.
      router.refresh();
      router.push(next);
    } catch {
      setError("Could not reach the server.");
    } finally {
      setPending(false);
    }
  }

  return (
    <form onSubmit={onSubmit} className="w-full max-w-sm space-y-4">
      <div>
        <h1 className="text-lg font-semibold">Crucible</h1>
        <p className="mt-1 text-sm text-muted">Evaluation and observability for AI agents.</p>
      </div>

      <label className="block">
        <span className="text-sm font-medium">Email</span>
        <input
          name="email"
          type="email"
          required
          autoComplete="username"
          autoFocus
          className="mt-1 w-full rounded border bg-panel px-2.5 py-1.5 text-sm outline-none focus:border-accent"
        />
      </label>

      <label className="block">
        <span className="text-sm font-medium">Password</span>
        <input
          name="password"
          type="password"
          required
          autoComplete="current-password"
          className="mt-1 w-full rounded border bg-panel px-2.5 py-1.5 text-sm outline-none focus:border-accent"
        />
      </label>

      {error && (
        <p role="alert" className="rounded border border-fail/40 bg-fail-bg px-2.5 py-1.5 text-sm text-fail">
          {error}
        </p>
      )}

      <button
        type="submit"
        disabled={pending}
        className="w-full rounded bg-text px-3 py-1.5 text-sm font-medium text-bg disabled:opacity-50"
      >
        {pending ? "Signing in…" : "Sign in"}
      </button>
    </form>
  );
}

export default function LoginPage() {
  return (
    <main className="flex min-h-screen items-center justify-center px-4">
      {/* `useSearchParams` opts the route into client rendering; the boundary keeps that local. */}
      <Suspense>
        <LoginForm />
      </Suspense>
    </main>
  );
}
