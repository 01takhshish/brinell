import Link from "next/link";

import { EmptyState, Panel } from "@/components/primitives";
import { RunHeader } from "@/components/run-header";
import { getClusters, getRun } from "@/lib/api";
import type { FailureCluster } from "@/lib/types";

export const metadata = { title: "Clusters" };

/**
 * Failures grouped by similarity, so a run with sixty red rows reads as the three bugs it
 * probably is. The grouping is computed by the worker and only read here — embedding sixty
 * failures on page load would put a variable-cost operation behind a page view.
 */
export default async function ClustersPage({
  params,
}: {
  params: Promise<{ projectId: string; runId: string }>;
}) {
  const { projectId, runId } = await params;
  const [run, { clusters }] = await Promise.all([getRun(projectId, runId), getClusters(projectId, runId)]);

  const clustered = clusters.reduce((total, cluster) => total + cluster.size, 0);

  return (
    <>
      <RunHeader run={run} projectId={projectId} active="/clusters" />

      {clusters.length === 0 ? (
        <Panel>
          <EmptyState title="No clusters for this run">
            {run.passed_cases === run.total_cases && run.total_cases > 0
              ? "Every case passed, so there is nothing to group."
              : "Clustering has not been computed yet. Run crucible cluster, or POST to this run's /clusters endpoint."}
          </EmptyState>
        </Panel>
      ) : (
        <>
          <p className="mb-4 text-sm text-muted">
            <span className="num">{clustered}</span> failing{" "}
            {clustered === 1 ? "case falls" : "cases fall"} into{" "}
            <span className="num">{clusters.length}</span>{" "}
            {clusters.length === 1 ? "group" : "groups"}.
          </p>
          <div className="space-y-4">
            {clusters.map((cluster) => (
              <ClusterCard key={cluster.id} cluster={cluster} projectId={projectId} runId={runId} />
            ))}
          </div>
        </>
      )}
    </>
  );
}

function ClusterCard({
  cluster,
  projectId,
  runId,
}: {
  cluster: FailureCluster;
  projectId: string;
  runId: string;
}) {
  return (
    <Panel
      title={
        <span className="flex flex-wrap items-center gap-2">
          <span>{cluster.label ?? "Unlabelled cluster"}</span>
          <span className="num rounded bg-fail-bg px-1.5 py-0.5 text-xs font-normal text-fail">
            {cluster.size} {cluster.size === 1 ? "case" : "cases"}
          </span>
        </span>
      }
    >
      <div className="space-y-3 px-4 py-3">
        {cluster.summary && <p className="text-sm text-muted">{cluster.summary}</p>}

        {cluster.top_terms.length > 0 && (
          <ul className="flex flex-wrap gap-1.5">
            {cluster.top_terms.map((term) => (
              <li key={term} className="rounded border px-1.5 py-0.5 font-mono text-xs text-muted">
                {term}
              </li>
            ))}
          </ul>
        )}

        <ul className="flex flex-wrap gap-x-4 gap-y-1">
          {cluster.members.map((member) => (
            <li key={member.run_case_id}>
              <Link
                href={`/projects/${projectId}/runs/${runId}/cases/${member.run_case_id}`}
                className="font-mono text-xs text-accent hover:underline"
                // Distance is the reason this case is in this group rather than another one, so it
                // belongs somewhere reachable — but not in the middle of a list you are scanning.
                title={`distance ${member.distance}`}
              >
                {member.external_id}
              </Link>
            </li>
          ))}
        </ul>
      </div>
    </Panel>
  );
}
