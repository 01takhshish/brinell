import { NextResponse } from "next/server";

import { readDetail } from "@/lib/api-error";

const API_URL = (process.env.CRUCIBLE_API_URL ?? "http://127.0.0.1:8000").replace(/\/+$/, "");

/**
 * Exchanges credentials for a session, then forwards the API's own `Set-Cookie` verbatim.
 *
 * Minting a second cookie here would mean this app deciding `httponly`, `samesite` and `secure`
 * for itself, and quietly disagreeing with the API the first time one of those settings changed.
 * Passing the header through keeps the API the single authority on what a session looks like.
 * There is no `Domain` attribute on it, so the cookie binds to whichever host served this
 * response — this app — which is the only host the browser ever talks to.
 */
export async function POST(request: Request) {
  let credentials: { email?: unknown; password?: unknown };
  try {
    credentials = await request.json();
  } catch {
    return NextResponse.json({ detail: "expected a JSON body" }, { status: 400 });
  }

  const { email, password } = credentials;
  if (typeof email !== "string" || typeof password !== "string") {
    return NextResponse.json({ detail: "email and password are required" }, { status: 400 });
  }

  let upstream: Response;
  try {
    upstream = await fetch(`${API_URL}/api/v1/auth/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password }),
      cache: "no-store",
    });
  } catch {
    return NextResponse.json({ detail: `cannot reach the Crucible API at ${API_URL}` }, { status: 502 });
  }

  if (!upstream.ok) {
    // Upstream wording is reused rather than replaced: "Invalid credentials" is already the right
    // answer, and inventing a friendlier one here would mean two places to keep honest.
    return NextResponse.json({ detail: await readDetail(upstream) }, { status: upstream.status });
  }

  const response = NextResponse.json({ ok: true });
  for (const cookie of upstream.headers.getSetCookie()) {
    response.headers.append("set-cookie", cookie);
  }
  return response;
}
