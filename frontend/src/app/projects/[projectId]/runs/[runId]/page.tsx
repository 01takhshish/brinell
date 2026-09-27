import Link from "next/link";

import { CaseStatusBadge } from "@/components/badges";
import { EmptyState, Panel, Stat, Td, Th } from "@/components/primitives";
import { RunHeader } from "@/components/run-header";
import { getRun, listRunCases } from "@/lib/api";
import { formatDuration, formatPercent, formatScore, formatTokens, formatUsd } from "@/lib/format";

export const metadata = { title: "Run" };

const FILTERS = [
  { key: "all", label: "all" },
  { key: "failed", label: "failed" },
  { key: "errored", label: "errored" },
  { key: "passed", label: "passed" },
] as const;

type FilterKey = (typeof FILTERS)[number]["key"];

export default async function RunPage({
  params,
  searchParams,
}: {
  params: Promise<{ projectId: string; runId: string }>;
  searchParams: Promise<{ cases?: string }>;
}) {
  const { projectId, runId } = await params;
  const { cases: filterParam } = await searchParams;
  const filter: FilterKey =
    FILTERS.find((f) => f.key === filterParam)?.key ?? ("all" as FilterKey);

  const [run, casePage] = await Promise.all([getRun(projectId, runId), listRunCases(projectId, runId)]);

  // The endpoint caps a page at 1000 cases. Suites that large are rare but not hypothetical, and the
  // filter counts below are computed over `items` — so when the page is short of the total, say so
  // rather than letting the numbers read as if they covered the whole run.
  const allCases = casePage.items;
  const truncated = casePage.total > allCases.length;

  const shown = allCases.filter((c) =>
    filter === "all"
      ? true
      : filter === "failed"
        ? c.status === "failed"
        : filter === "errored"
          ? c.status === "error"
          : c.status === "passed",
  );

  const wallClock =
    run.started_at && run.finished_at
      ? Date.parse(`${run.finished_at}Z`) - Date.parse(`${run.started_at}Z`)
      : null;

  return (
    <>
      <RunHeader run={run} projectId={projectId} active="" />

      <div className="mb-5 grid grid-cols-2 gap-3 md:grid-cols-3 lg:grid-cols-6">
        <Stat
          label="Pass rate"
          value={formatPercent(run.pass_rate)}
          hint={`${run.passed_cases} of ${run.total_cases}`}
          tone={run.pass_rate === null ? "default" : run.pass_rate === 1 ? "pass" : "fail"}
        />
        <Stat
          label="Errored"
          value={run.errored_cases}
          hint="no verdict reached"
          tone={run.errored_cases > 0 ? "warn" : "default"}
        />
        <Stat
          label="Cost"
          value={formatUsd(run.total_cost_micros)}
          hint={`${formatTokens(run.total_input_tokens)} in · ${formatTokens(run.total_output_tokens)} out`}
        />
        <Stat label="p50 latency" value={formatDuration(run.p50_latency_ms)} hint="per case" />
        <Stat label="p95 latency" value={formatDuration(run.p95_latency_ms)} hint="per case" />
        <Stat
          label="Cache hits"
          value={run.cache_hits}
          hint={wallClock === null ? "—" : `${formatDuration(wallClock)} wall clock`}
        />
      </div>

      <Panel
        title={`Cases (${shown.length}${filter === "all" ? "" : ` of ${allCases.length}`})${
          truncated ? ` — first ${allCases.length} of ${casePage.total}` : ""
        }`}
        action={
          <nav className="flex gap-1">
            {FILTERS.map((option) => {
              const href =
                option.key === "all"
                  ? `/projects/${projectId}/runs/${runId}`
                  : `/projects/${projectId}/runs/${runId}?cases=${option.key}`;
              return (
                <Link
                  key={option.key}
                  href={href}
                  className={`rounded px-2 py-0.5 text-xs ${
                    filter === option.key ? "bg-text text-bg" : "text-muted hover:bg-bg hover:text-text"
                  }`}
                >
                  {option.label}
                </Link>
              );
            })}
          </nav>
        }
      >
        {shown.length === 0 ? (
          <EmptyState title={filter === "all" ? "No cases" : `No ${filter} cases`}>
            {filter === "failed" && allCases.length > 0 ? "Everything passed." : undefined}
          </EmptyState>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[48rem]">
              <thead>
                <tr>
                  <Th>Case</Th>
                  <Th>Status</Th>
                  <Th className="text-right">Score</Th>
                  <Th className="text-right">Latency</Th>
                  <Th className="text-right">Cost</Th>
                  <Th className="text-right">Attempt</Th>
                  <Th>Detail</Th>
                </tr>
              </thead>
              <tbody>
                {shown.map((runCase) => (
                  <tr key={runCase.id} className="last:[&>td]:border-b-0 hover:bg-bg">
                    <Td>
                      <Link
                        href={`/projects/${projectId}/runs/${runId}/cases/${runCase.id}`}
                        className="font-mono text-xs text-accent hover:underline"
                      >
                        {runCase.external_id}
                      </Link>
                    </Td>
                    <Td>
                      <CaseStatusBadge status={runCase.status} />
                    </Td>
                    <Td className="num text-right">{formatScore(runCase.score)}</Td>
                    <Td className="num text-right text-muted">{formatDuration(runCase.latency_ms)}</Td>
                    <Td className="num text-right text-muted">{formatUsd(runCase.cost_micros)}</Td>
                    <Td className="num text-right text-muted">
                      {/* Only worth showing when it is not 1 — a column of "1" is noise. */}
                      {runCase.attempt > 1 ? runCase.attempt : "—"}
                    </Td>
                    <Td className="text-xs text-muted">
                      {runCase.cache_hit && <span className="mr-2 text-info">cached</span>}
                      {runCase.error && <span className="wrap-anywhere font-mono text-fail">{runCase.error}</span>}
                    </Td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
    </>
  );
}
