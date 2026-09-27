import Link from "next/link";

import { RunStatusBadge } from "@/components/badges";
import { LiveRefresh } from "@/components/live-refresh";
import { PassBar } from "@/components/pass-bar";
import { EmptyState, Panel, Td, Th } from "@/components/primitives";
import { RelativeTime } from "@/components/relative-time";
import { listRuns } from "@/lib/api";
import { formatTokens, formatUsd, shortId, shortSha } from "@/lib/format";
import { LIVE_RUN_STATUSES, RUN_STATUSES, type RunStatus } from "@/lib/types";

export const metadata = { title: "Runs" };

// Matches the API default. The endpoint caps `limit` at 200, so this is a page size, not a ceiling
// on what the project can hold — the pager below the table walks the rest.
const PAGE_SIZE = 50;

function isRunStatus(value: string | undefined): value is RunStatus {
  return value !== undefined && (RUN_STATUSES as readonly string[]).includes(value);
}

export default async function RunsPage({
  params,
  searchParams,
}: {
  params: Promise<{ projectId: string }>;
  searchParams: Promise<{ status?: string; page?: string }>;
}) {
  const { projectId } = await params;
  const { status, page: pageParam } = await searchParams;
  const filter = isRunStatus(status) ? status : undefined;

  // `Number.parseInt` on absent or junk input gives NaN, which `Math.max` propagates — so clamp
  // against NaN explicitly rather than trusting the query string to be a number.
  const parsed = Number.parseInt(pageParam ?? "1", 10);
  const pageNumber = Number.isFinite(parsed) && parsed > 0 ? parsed : 1;
  const offset = (pageNumber - 1) * PAGE_SIZE;

  const runs = await listRuns(projectId, { status: filter, limit: PAGE_SIZE, offset });
  const shown = runs.items;
  const lastPage = Math.max(1, Math.ceil(runs.total / PAGE_SIZE));

  // Poll only while something can still change. A page of finished runs is a static page.
  const anyLive = shown.some((run) => LIVE_RUN_STATUSES.includes(run.status));

  const pageHref = (n: number) => {
    const query = new URLSearchParams();
    if (filter) query.set("status", filter);
    if (n > 1) query.set("page", String(n));
    const suffix = query.toString();
    return `/projects/${projectId}/runs${suffix ? `?${suffix}` : ""}`;
  };

  return (
    <>
      <header className="mb-4 flex flex-wrap items-center gap-3">
        <h1 className="text-lg font-semibold">Runs</h1>
        <LiveRefresh active={anyLive} />
        <nav className="ml-auto flex flex-wrap gap-1">
          {[undefined, ...RUN_STATUSES].map((option) => {
            // Deliberately without `page`: switching filters returns to the first page, because
            // page 7 of "all runs" has nothing to do with page 7 of "failed runs".
            const href = option
              ? `/projects/${projectId}/runs?status=${option}`
              : `/projects/${projectId}/runs`;
            const active = filter === option;
            return (
              <Link
                key={option ?? "all"}
                href={href}
                className={`rounded px-2 py-1 text-xs ${
                  active ? "bg-text text-bg" : "text-muted hover:bg-panel hover:text-text"
                }`}
              >
                {option ?? "all"}
              </Link>
            );
          })}
        </nav>
      </header>

      <Panel>
        {shown.length === 0 ? (
          <EmptyState
            title={
              offset > 0
                ? "Nothing on this page"
                : filter
                  ? `No ${filter} runs`
                  : "No runs yet"
            }
          >
            {offset > 0 ? (
              <Link href={pageHref(1)} className="underline">
                Back to the first page
              </Link>
            ) : (
              <>
                Start one with{" "}
                <code className="font-mono text-xs">crucible run --suite default</code>.
              </>
            )}
          </EmptyState>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[56rem]">
              <thead>
                <tr>
                  <Th>Run</Th>
                  <Th>Status</Th>
                  <Th>Pass rate</Th>
                  <Th className="text-right">Cost</Th>
                  <Th className="text-right">Tokens</Th>
                  <Th className="text-right">Cache</Th>
                  <Th>Source</Th>
                  <Th>Started</Th>
                </tr>
              </thead>
              <tbody>
                {shown.map((run) => (
                  <tr key={run.id} className="last:[&>td]:border-b-0 hover:bg-bg">
                    <Td>
                      <Link
                        href={`/projects/${projectId}/runs/${run.id}`}
                        className="font-medium text-accent hover:underline"
                      >
                        {run.label ?? shortId(run.id)}
                      </Link>
                      {run.label && <div className="num mt-0.5 text-xs text-faint">{shortId(run.id)}</div>}
                    </Td>
                    <Td>
                      <RunStatusBadge status={run.status} />
                    </Td>
                    <Td>
                      <PassBar
                        passed={run.passed_cases}
                        errored={run.errored_cases}
                        total={run.total_cases}
                        rate={run.pass_rate}
                      />
                      {run.status === "running" && (
                        <div className="num mt-0.5 text-xs text-faint">
                          {run.completed_cases}/{run.total_cases} complete
                        </div>
                      )}
                    </Td>
                    <Td className="num text-right">{formatUsd(run.total_cost_micros)}</Td>
                    <Td className="num text-right text-muted">
                      {formatTokens(run.total_input_tokens + run.total_output_tokens)}
                    </Td>
                    <Td className="num text-right text-muted">{run.cache_hits || "—"}</Td>
                    <Td className="text-xs text-muted">
                      <span>{run.trigger}</span>
                      {run.git_sha && (
                        <span className="num ml-1.5 text-faint">
                          {run.git_branch ? `${run.git_branch}@` : ""}
                          {shortSha(run.git_sha)}
                        </span>
                      )}
                    </Td>
                    <Td className="text-muted">
                      <RelativeTime iso={run.started_at ?? run.created_at} />
                    </Td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>

      {runs.total > PAGE_SIZE && (
        <nav className="mt-3 flex items-center gap-3 text-xs text-muted">
          <span className="num">
            {offset + 1}–{offset + shown.length} of {runs.total}
          </span>
          <span className="ml-auto flex gap-1">
            <PageLink href={pageHref(pageNumber - 1)} disabled={pageNumber <= 1}>
              ← Newer
            </PageLink>
            <PageLink href={pageHref(pageNumber + 1)} disabled={pageNumber >= lastPage}>
              Older →
            </PageLink>
          </span>
        </nav>
      )}
    </>
  );
}

/** A pager arrow. Disabled renders as a span, not a dead link — nothing to tab to, nothing to click. */
function PageLink({
  href,
  disabled,
  children,
}: {
  href: string;
  disabled: boolean;
  children: React.ReactNode;
}) {
  const className = "rounded px-2 py-1";
  if (disabled) return <span className={`${className} text-faint`}>{children}</span>;
  return (
    <Link href={href} className={`${className} hover:bg-panel hover:text-text`}>
      {children}
    </Link>
  );
}
