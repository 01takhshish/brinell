import Link from "next/link";

import { VerdictBadge } from "@/components/badges";
import { EmptyState, ErrorState, Panel, Stat, Td, Th } from "@/components/primitives";
import { RunHeader } from "@/components/run-header";
import { ApiError, getComparison, getRun } from "@/lib/api";
import { formatDuration, formatPercent, formatScore, formatUsdDelta, shortId } from "@/lib/format";
import type { Comparison, Verdict } from "@/lib/types";

export const metadata = { title: "Comparison" };

/** Ordered by how much they should worry you, because that is the order you want to read them in. */
const VERDICT_ORDER: Verdict[] = ["broken", "regressed", "flaky", "fixed", "improved", "added", "removed", "unchanged"];

export default async function ComparePage({
  params,
  searchParams,
}: {
  params: Promise<{ projectId: string; runId: string }>;
  searchParams: Promise<{ baseline?: string; show?: string }>;
}) {
  const { projectId, runId } = await params;
  const { baseline, show } = await searchParams;
  const run = await getRun(projectId, runId);

  let comparison: Comparison | null = null;
  let failure: string | null = null;
  try {
    comparison = await getComparison(projectId, runId, baseline);
  } catch (error) {
    if (!(error instanceof ApiError)) throw error;
    failure = error.detail;
  }

  return (
    <>
      <RunHeader run={run} projectId={projectId} active="/compare" />

      {comparison === null ? (
        <Panel>
          <EmptyState title="Nothing to compare against">
            {failure ?? "This run has no baseline."} Pick one by appending{" "}
            <code className="font-mono text-xs">?baseline=&lt;run id&gt;</code> to the URL, or start runs with{" "}
            <code className="font-mono text-xs">--baseline</code> so CI can gate on the difference.
          </EmptyState>
        </Panel>
      ) : (
        <ComparisonBody comparison={comparison} projectId={projectId} runId={runId} show={show} />
      )}
    </>
  );
}

function ComparisonBody({
  comparison,
  projectId,
  runId,
  show,
}: {
  comparison: Comparison;
  projectId: string;
  runId: string;
  show?: string;
}) {
  const { summary } = comparison;
  // Default to the rows that changed. A 500-case suite where three things moved should open on
  // those three, not on 497 lines of "unchanged".
  const showAll = show === "all";
  const deltas = [...comparison.deltas]
    .filter((d) => showAll || d.verdict !== "unchanged")
    .sort((a, b) => VERDICT_ORDER.indexOf(a.verdict) - VERDICT_ORDER.indexOf(b.verdict));

  const passRateDelta = comparison.pass_rate_delta;

  return (
    <>
      {comparison.has_regressions ? (
        <div className="mb-5">
          <ErrorState
            title={`${summary.broken} ${summary.broken === 1 ? "case" : "cases"} that passed on the baseline now fail`}
            detail="This is what `crucible run --fail-on-regression` exits non-zero on."
          />
        </div>
      ) : (
        <p className="mb-5 rounded-lg border border-pass/40 bg-pass-bg px-4 py-3 text-sm text-pass">
          No regressions. Nothing that passed on the baseline fails here.
        </p>
      )}

      <div className="mb-5 grid grid-cols-2 gap-3 md:grid-cols-3 lg:grid-cols-6">
        <Stat label="Broken" value={summary.broken} tone={summary.broken > 0 ? "fail" : "default"} hint="passed → failed" />
        <Stat label="Fixed" value={summary.fixed} tone={summary.fixed > 0 ? "pass" : "default"} hint="failed → passed" />
        <Stat label="Regressed" value={summary.regressed} tone={summary.regressed > 0 ? "warn" : "default"} hint="score fell" />
        <Stat label="Improved" value={summary.improved} tone={summary.improved > 0 ? "pass" : "default"} hint="score rose" />
        <Stat
          label="Pass rate"
          value={passRateDelta === null ? "—" : `${passRateDelta >= 0 ? "+" : "−"}${formatPercent(Math.abs(passRateDelta))}`}
          tone={passRateDelta === null || passRateDelta === 0 ? "default" : passRateDelta > 0 ? "pass" : "fail"}
          hint="vs baseline"
        />
        <Stat
          label="Cost"
          value={formatUsdDelta(comparison.cost_delta_micros)}
          tone={comparison.cost_delta_micros > 0 ? "warn" : "default"}
          hint="vs baseline"
        />
      </div>

      <p className="mb-3 text-xs text-muted">
        Baseline{" "}
        <Link
          href={`/projects/${projectId}/runs/${comparison.baseline_run_id}`}
          className="num text-accent hover:underline"
        >
          {shortId(comparison.baseline_run_id)}
        </Link>{" "}
        · {summary.unchanged} unchanged, {summary.added} added, {summary.removed} removed
      </p>

      <Panel
        title={`Changed cases (${deltas.length})`}
        action={
          <Link
            href={
              showAll
                ? `/projects/${projectId}/runs/${runId}/compare`
                : `/projects/${projectId}/runs/${runId}/compare?show=all`
            }
            className="text-xs text-accent hover:underline"
          >
            {showAll ? "hide unchanged" : "show all cases"}
          </Link>
        }
      >
        {deltas.length === 0 ? (
          <EmptyState title="Every case landed exactly as it did on the baseline" />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[48rem]">
              <thead>
                <tr>
                  <Th>Case</Th>
                  <Th>Verdict</Th>
                  <Th>Baseline</Th>
                  <Th>Current</Th>
                  <Th className="text-right">Score Δ</Th>
                  <Th className="text-right">Cost Δ</Th>
                  <Th className="text-right">Latency Δ</Th>
                  <Th>Detail</Th>
                </tr>
              </thead>
              <tbody>
                {deltas.map((delta) => (
                  <tr key={delta.external_id} className="last:[&>td]:border-b-0 hover:bg-bg">
                    <Td className="font-mono text-xs">{delta.external_id}</Td>
                    <Td>
                      <VerdictBadge verdict={delta.verdict} />
                    </Td>
                    <Td className="text-muted">
                      {delta.baseline_status ?? "—"}{" "}
                      <span className="num text-xs text-faint">{formatScore(delta.baseline_score)}</span>
                    </Td>
                    <Td className="text-muted">
                      {delta.current_status ?? "—"}{" "}
                      <span className="num text-xs text-faint">{formatScore(delta.current_score)}</span>
                    </Td>
                    <Td className="num text-right">
                      {delta.score_delta === null
                        ? "—"
                        : `${delta.score_delta >= 0 ? "+" : "−"}${formatScore(Math.abs(delta.score_delta))}`}
                    </Td>
                    <Td className="num text-right text-muted">{formatUsdDelta(delta.cost_delta_micros)}</Td>
                    <Td className="num text-right text-muted">
                      {delta.latency_delta_ms === null
                        ? "—"
                        : `${delta.latency_delta_ms >= 0 ? "+" : "−"}${formatDuration(Math.abs(delta.latency_delta_ms))}`}
                    </Td>
                    <Td className="wrap-anywhere text-xs text-muted">{delta.detail ?? "—"}</Td>
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
