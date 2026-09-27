import "server-only";

import { cookies } from "next/headers";
import { redirect } from "next/navigation";

import { readDetail } from "@/lib/api-error";
import type {
  ClusterResponse,
  Comparison,
  Page,
  Project,
  Run,
  RunCase,
  RunCaseDetail,
  RunStatus,
} from "@/lib/types";

/**
 * The browser never talks to the Crucible API directly.
 *
 * The session is an HttpOnly cookie, and the API's CORS policy admits exactly one origin, so a
 * browser-side `fetch` would need either a relaxed CORS policy or a token in reach of any script
 * on the page. Instead every read happens in a server component, which forwards the cookie
 * server-to-server. The token stays HttpOnly, CORS stays shut, and the browser is handed HTML.
 */
const API_URL = (process.env.CRUCIBLE_API_URL ?? "http://127.0.0.1:8000").replace(/\/+$/, "");
const SESSION_COOKIE = "crucible_session";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly detail: string,
    readonly path: string,
  ) {
    super(`${status} on ${path}: ${detail}`);
    this.name = "ApiError";
  }
}

async function sessionHeader(): Promise<Record<string, string>> {
  const token = (await cookies()).get(SESSION_COOKIE)?.value;
  return token ? { Cookie: `${SESSION_COOKIE}=${token}` } : {};
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const url = `${API_URL}/api/v1${path}`;
  let response: Response;
  try {
    response = await fetch(url, {
      ...init,
      headers: { Accept: "application/json", ...(await sessionHeader()), ...init.headers },
      // Run data changes while you are looking at it; a cached runs list is a wrong runs list.
      cache: "no-store",
    });
  } catch {
    // A connection refused here almost always means the API is not running, which is worth
    // saying plainly rather than surfacing as an opaque "fetch failed" in a red box.
    throw new ApiError(0, `cannot reach the Crucible API at ${API_URL}`, path);
  }

  if (response.status === 401) {
    // Expired or absent session. Nothing on this page is renderable, so bounce to the login form
    // rather than rendering a shell full of error states.
    redirect("/login");
  }
  if (!response.ok) {
    throw new ApiError(response.status, await readDetail(response), path);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

/** For pages that want to render a signed-out state instead of redirecting into one. */
export async function hasSession(): Promise<boolean> {
  return (await cookies()).get(SESSION_COOKIE) !== undefined;
}

export const listProjects = () => request<Project[]>("/projects");

export const getProject = (projectId: string) => request<Project>(`/projects/${projectId}`);

export function listRuns(
  projectId: string,
  options: { status?: RunStatus; suiteId?: string; limit?: number; offset?: number } = {},
): Promise<Page<Run>> {
  const query = new URLSearchParams();
  if (options.status) query.set("status", options.status);
  if (options.suiteId) query.set("suite_id", options.suiteId);
  query.set("limit", String(options.limit ?? 50));
  query.set("offset", String(options.offset ?? 0));
  return request<Page<Run>>(`/projects/${projectId}/runs?${query}`);
}

export const getRun = (projectId: string, runId: string) =>
  request<Run>(`/projects/${projectId}/runs/${runId}`);

export const listRunCases = (projectId: string, runId: string) =>
  request<Page<RunCase>>(`/projects/${projectId}/runs/${runId}/cases`);

export const getRunCase = (projectId: string, runId: string, caseId: string) =>
  request<RunCaseDetail>(`/projects/${projectId}/runs/${runId}/cases/${caseId}`);

export const getClusters = (projectId: string, runId: string) =>
  request<ClusterResponse>(`/projects/${projectId}/runs/${runId}/clusters`);

export function getComparison(projectId: string, runId: string, baseline?: string): Promise<Comparison> {
  const query = baseline ? `?baseline=${encodeURIComponent(baseline)}` : "";
  return request<Comparison>(`/projects/${projectId}/runs/${runId}/compare${query}`);
}

/**
 * Login is the one call the browser makes "directly" — via a route handler on this server, so the
 * API's `Set-Cookie` can be passed straight through to the browser without either side relaxing
 * its origin rules.
 */
export async function login(email: string, password: string): Promise<string> {
  const response = await fetch(`${API_URL}/api/v1/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
    cache: "no-store",
  });
  if (!response.ok) {
    throw new ApiError(response.status, await readDetail(response), "/auth/login");
  }
  const body = (await response.json()) as { access_token: string };
  return body.access_token;
}
