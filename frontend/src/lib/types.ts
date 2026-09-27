/**
 * Mirrors `crucible.api.schemas`. Hand-written rather than generated, because the generated
 * client for a twelve-endpoint API is more machinery than it saves — but it is a mirror, so
 * when the Python changes shape this file is the thing that has to change with it.
 */

export const RUN_STATUSES = ["queued", "running", "succeeded", "failed", "cancelled"] as const;
export type RunStatus = (typeof RUN_STATUSES)[number];

export const CASE_STATUSES = ["pending", "claimed", "passed", "failed", "error", "skipped"] as const;
export type CaseStatus = (typeof CASE_STATUSES)[number];

export const VERDICTS = [
  "broken",
  "fixed",
  "regressed",
  "improved",
  "unchanged",
  "added",
  "removed",
  "flaky",
] as const;
export type Verdict = (typeof VERDICTS)[number];

/** A run is live when the worker may still write to it; the UI polls only while this is true. */
export const LIVE_RUN_STATUSES: readonly RunStatus[] = ["queued", "running"];

export interface User {
  id: string;
  email: string;
  name: string;
  is_admin: boolean;
}

export interface Project {
  id: string;
  name: string;
  slug: string;
  role: string | null;
  created_at: string;
}

export interface Suite {
  id: string;
  name: string;
  dataset_id: string;
  assertions: Record<string, unknown>[];
  judge_model: string | null;
  pass_threshold: number;
  created_at: string;
}

export interface Run {
  id: string;
  status: RunStatus;
  label: string | null;
  trigger: string;
  git_sha: string | null;
  git_branch: string | null;
  target_version_id: string;
  suite_id: string;
  dataset_version_id: string;
  baseline_run_id: string | null;
  total_cases: number;
  completed_cases: number;
  passed_cases: number;
  errored_cases: number;
  pass_rate: number | null;
  mean_score: number | null;
  total_cost_micros: number;
  total_input_tokens: number;
  total_output_tokens: number;
  cache_hits: number;
  p50_latency_ms: number | null;
  p95_latency_ms: number | null;
  p99_latency_ms: number | null;
  error: string | null;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
}

export interface RunCase {
  id: string;
  external_id: string;
  status: CaseStatus;
  score: number | null;
  attempt: number;
  latency_ms: number | null;
  cost_micros: number | null;
  cache_hit: boolean;
  error: string | null;
  finished_at: string | null;
}

/** One assertion's verdict. The runner writes these as free-form dicts, so the fields are optional. */
export interface AssertionResult {
  type?: string;
  passed?: boolean;
  score?: number;
  detail?: string;
  expected?: unknown;
  actual?: unknown;
  [key: string]: unknown;
}

/** One step of the agent's execution — a model call or a tool call. */
export interface Span {
  name?: string;
  kind?: string;
  started_at?: string;
  ended_at?: string;
  duration_ms?: number;
  input?: unknown;
  output?: unknown;
  model?: string;
  input_tokens?: number;
  output_tokens?: number;
  cost_micros?: number;
  cache_hit?: boolean;
  error?: string | null;
  [key: string]: unknown;
}

export interface RunCaseDetail extends RunCase {
  input: Record<string, unknown>;
  expected: Record<string, unknown> | null;
  output: Record<string, unknown> | null;
  assertion_results: AssertionResult[];
  spans: Span[];
}

export interface CaseDelta {
  external_id: string;
  verdict: Verdict;
  baseline_status: string | null;
  current_status: string | null;
  baseline_score: number | null;
  current_score: number | null;
  score_delta: number | null;
  cost_delta_micros: number | null;
  latency_delta_ms: number | null;
  detail: string | null;
}

export interface ComparisonSummary {
  broken: number;
  fixed: number;
  regressed: number;
  improved: number;
  unchanged: number;
  added: number;
  removed: number;
}

export interface Comparison {
  baseline_run_id: string;
  current_run_id: string;
  baseline: Record<string, unknown>;
  current: Record<string, unknown>;
  summary: ComparisonSummary;
  pass_rate_delta: number | null;
  cost_delta_micros: number;
  has_regressions: boolean;
  deltas: CaseDelta[];
}

export interface ClusterMember {
  run_case_id: string;
  external_id: string;
  status: string;
  distance: number;
}

export interface FailureCluster {
  id: string;
  label: string | null;
  summary: string | null;
  size: number;
  top_terms: string[];
  members: ClusterMember[];
}

export interface ClusterResponse {
  run_id: string;
  clusters: FailureCluster[];
}

/**
 * The shape every list endpoint returns. `total` counts the rows matching the filter, not the rows
 * in `items` — that gap is the whole point of it being here. A list route that returns only the
 * page cannot tell a caller whether it is looking at everything, and the UI has no way to guess.
 */
export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}
