import Link from "next/link";

import { CaseStatusBadge } from "@/components/badges";
import { JsonBlock } from "@/components/json-block";
import { Breadcrumbs, EmptyState, Panel, Stat } from "@/components/primitives";
import { Trace } from "@/components/trace";
import { getRun, getRunCase } from "@/lib/api";
import { formatDuration, formatScore, formatUsd, shortId } from "@/lib/format";
import type { AssertionResult } from "@/lib/types";

export const metadata = { title: "Case" };

export default async function CasePage({
  params,
}: {
  params: Promise<{ projectId: string; runId: string; caseId: string }>;
}) {
  const { projectId, runId, caseId } = await params;
  const [run, runCase] = await Promise.all([getRun(projectId, runId), getRunCase(projectId, runId, caseId)]);

  return (
    <>
      <header className="mb-5">
        <Breadcrumbs
          items={[
            { label: "Runs", href: `/projects/${projectId}/runs` },
            { label: run.label ?? shortId(run.id), href: `/projects/${projectId}/runs/${runId}` },
            { label: runCase.external_id },
          ]}
        />
        <div className="mt-2 flex flex-wrap items-center gap-3">
          <h1 className="font-mono text-lg font-semibold">{runCase.external_id}</h1>
          <CaseStatusBadge status={runCase.status} />
          {runCase.cache_hit && <span className="text-xs text-info">served from cache</span>}
        </div>
      </header>

      {runCase.error && (
        <div className="mb-5 rounded-lg border border-fail/40 bg-fail-bg px-4 py-3">
          <p className="text-sm font-medium text-fail">The case did not reach a verdict</p>
          <p className="wrap-anywhere mt-1 font-mono text-xs text-fail/90">{runCase.error}</p>
        </div>
      )}

      <div className="mb-5 grid grid-cols-2 gap-3 md:grid-cols-4">
        <Stat
          label="Score"
          value={formatScore(runCase.score)}
          tone={runCase.status === "passed" ? "pass" : runCase.status === "failed" ? "fail" : "default"}
        />
        <Stat label="Latency" value={formatDuration(runCase.latency_ms)} />
        <Stat label="Cost" value={formatUsd(runCase.cost_micros)} />
        <Stat
          label="Attempt"
          value={runCase.attempt}
          hint={runCase.attempt > 1 ? "retried" : "first try"}
          tone={runCase.attempt > 1 ? "warn" : "default"}
        />
      </div>

      <div className="mb-5">
        <Panel title={`Assertions (${runCase.assertion_results.length})`}>
          {runCase.assertion_results.length === 0 ? (
            <EmptyState title="No assertion results recorded" />
          ) : (
            <ul className="divide-y">
              {runCase.assertion_results.map((assertion, index) => (
                <AssertionRow key={index} assertion={assertion} />
              ))}
            </ul>
          )}
        </Panel>
      </div>

      <div className="mb-5 grid gap-3 lg:grid-cols-3">
        <JsonBlock label="input" value={runCase.input} />
        <JsonBlock label="expected" value={runCase.expected} />
        <JsonBlock label="output" value={runCase.output} />
      </div>

      <Panel title={`Trace (${runCase.spans.length} ${runCase.spans.length === 1 ? "step" : "steps"})`}>
        {runCase.spans.length === 0 ? (
          <EmptyState title="No spans recorded">
            Spans are written as the agent runs; a case that failed before its first model call has none.
          </EmptyState>
        ) : (
          <Trace spans={runCase.spans} />
        )}
      </Panel>

      <p className="mt-4 text-xs text-muted">
        <Link href={`/projects/${projectId}/runs/${runId}/clusters`} className="text-accent hover:underline">
          See whether this failure is part of a cluster →
        </Link>
      </p>
    </>
  );
}

function AssertionRow({ assertion }: { assertion: AssertionResult }) {
  // `passed` is optional in the stored shape, so absence is rendered as unknown rather than as a
  // failure — an assertion whose result was never written is a different problem from one that failed.
  const passed = assertion.passed;
  const tone = passed === undefined ? "text-muted" : passed ? "text-pass" : "text-fail";
  const mark = passed === undefined ? "?" : passed ? "pass" : "fail";

  return (
    <li className="flex flex-wrap items-start gap-3 px-4 py-2.5">
      <span className={`w-10 shrink-0 text-xs font-medium ${tone}`}>{mark}</span>
      <span className="num w-44 shrink-0 text-sm">{assertion.type ?? "assertion"}</span>
      <span className="min-w-0 flex-1 text-sm text-muted">
        {assertion.detail ? (
          <span className="wrap-anywhere">{assertion.detail}</span>
        ) : (
          <span className="text-faint">—</span>
        )}
      </span>
      {assertion.score !== undefined && (
        <span className="num shrink-0 text-sm">{formatScore(assertion.score)}</span>
      )}
    </li>
  );
}
