import Link from "next/link";

import { RunStatusBadge } from "@/components/badges";
import { LiveRefresh } from "@/components/live-refresh";
import { Breadcrumbs } from "@/components/primitives";
import { RelativeTime } from "@/components/relative-time";
import { shortId, shortSha } from "@/lib/format";
import { LIVE_RUN_STATUSES, type Run } from "@/lib/types";

const TABS = [
  { key: "", label: "Cases" },
  { key: "/compare", label: "Comparison" },
  { key: "/clusters", label: "Clusters" },
] as const;

export function RunHeader({
  run,
  projectId,
  active,
}: {
  run: Run;
  projectId: string;
  active: (typeof TABS)[number]["key"];
}) {
  const base = `/projects/${projectId}/runs/${run.id}`;
  const live = LIVE_RUN_STATUSES.includes(run.status);

  return (
    <header className="mb-5">
      <Breadcrumbs
        items={[{ label: "Runs", href: `/projects/${projectId}/runs` }, { label: run.label ?? shortId(run.id) }]}
      />

      <div className="mt-2 flex flex-wrap items-center gap-3">
        <h1 className="text-lg font-semibold">{run.label ?? shortId(run.id)}</h1>
        <RunStatusBadge status={run.status} />
        <LiveRefresh active={live} />
        <div className="ml-auto flex items-center gap-3 text-xs text-muted">
          <span>{run.trigger}</span>
          {run.git_sha && (
            <span className="num">
              {run.git_branch ? `${run.git_branch}@` : ""}
              {shortSha(run.git_sha)}
            </span>
          )}
          <RelativeTime iso={run.started_at ?? run.created_at} />
        </div>
      </div>

      {run.error && (
        <p className="wrap-anywhere mt-2 rounded border border-fail/40 bg-fail-bg px-2.5 py-1.5 font-mono text-xs text-fail">
          {run.error}
        </p>
      )}

      <nav className="mt-4 flex gap-1 border-b">
        {TABS.map((tab) => {
          const selected = tab.key === active;
          return (
            <Link
              key={tab.key}
              href={`${base}${tab.key}`}
              aria-current={selected ? "page" : undefined}
              className={`-mb-px border-b-2 px-3 py-1.5 text-sm ${
                selected
                  ? "border-accent font-medium text-text"
                  : "border-transparent text-muted hover:text-text"
              }`}
            >
              {tab.label}
            </Link>
          );
        })}
      </nav>
    </header>
  );
}
