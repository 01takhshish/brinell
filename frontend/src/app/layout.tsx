import type { Metadata } from "next";
import type { ReactNode } from "react";

import "./globals.css";

/**
 * Every page in this app reads live run state through a per-user session cookie, so there is
 * nothing here that can be correctly prerendered at build time — an attempt to do so either bakes
 * in one user's view or, as it did here, fails the build because the API is not running on the
 * build machine. Declaring it once at the root is more honest than scattering the same escape
 * hatch through every route.
 */
export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: { default: "Crucible", template: "%s · Crucible" },
  description: "Evaluation and observability for AI agents — runs, regressions, traces and cost.",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen antialiased">{children}</body>
    </html>
  );
}
