import { cookies } from "next/headers";
import { NextResponse } from "next/server";

const API_URL = (process.env.CRUCIBLE_API_URL ?? "http://127.0.0.1:8000").replace(/\/+$/, "");
const SESSION_COOKIE = "crucible_session";

export async function POST() {
  const jar = await cookies();
  const token = jar.get(SESSION_COOKIE)?.value;

  // Best effort: the API revokes its side, but a network failure here must not leave someone
  // stuck holding a cookie they asked to be rid of, so the local clear happens either way.
  if (token) {
    try {
      await fetch(`${API_URL}/api/v1/auth/logout`, {
        method: "POST",
        headers: { Cookie: `${SESSION_COOKIE}=${token}` },
        cache: "no-store",
      });
    } catch {
      /* fall through to clearing the cookie */
    }
  }

  const response = NextResponse.json({ ok: true });
  response.cookies.set(SESSION_COOKIE, "", { path: "/", maxAge: 0 });
  return response;
}
