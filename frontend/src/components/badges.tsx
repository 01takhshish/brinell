import type { CaseStatus, RunStatus, Verdict } from "@/lib/types";

const BASE = "inline-flex items-center gap-1.5 rounded px-1.5 py-0.5 text-xs font-medium whitespace-nowrap";

const RUN_TONE: Record<RunStatus, string> = {
  queued: "bg-info-bg text-info",
  running: "bg-info-bg text-info",
  succeeded: "bg-pass-bg text-pass",
  failed: "bg-fail-bg text-fail",
  cancelled: "bg-warn-bg text-warn",
};

const CASE_TONE: Record<CaseStatus, string> = {
  pending: "bg-info-bg text-info",
  claimed: "bg-info-bg text-info",
  passed: "bg-pass-bg text-pass",
  failed: "bg-fail-bg text-fail",
  // An `error` is not a `failed`: the case never got a verdict, so it is neither a pass nor a
  // real failure, and colouring it like a failure would inflate the thing people gate CI on.
  error: "bg-warn-bg text-warn",
  skipped: "bg-warn-bg text-warn",
};

/**
 * Verdict colour answers "is this good news?", which is not the same as "did it pass":
 * `removed` is neutral, `flaky` is a warning, and `fixed` is the only green that means someone
 * did something right.
 */
const VERDICT_TONE: Record<Verdict, string> = {
  broken: "bg-fail-bg text-fail",
  regressed: "bg-warn-bg text-warn",
  fixed: "bg-pass-bg text-pass",
  improved: "bg-pass-bg text-pass",
  unchanged: "bg-transparent text-muted",
  added: "bg-info-bg text-info",
  removed: "bg-transparent text-muted",
  flaky: "bg-warn-bg text-warn",
};

function Pulse() {
  return (
    <span className="relative flex size-1.5">
      <span className="absolute inline-flex size-full animate-ping rounded-full bg-info opacity-70" />
      <span className="relative inline-flex size-1.5 rounded-full bg-info" />
    </span>
  );
}

export function RunStatusBadge({ status }: { status: RunStatus }) {
  const live = status === "running" || status === "queued";
  return (
    <span className={`${BASE} ${RUN_TONE[status] ?? "bg-transparent text-muted"}`}>
      {live && <Pulse />}
      {status}
    </span>
  );
}

export function CaseStatusBadge({ status }: { status: CaseStatus }) {
  return <span className={`${BASE} ${CASE_TONE[status] ?? "bg-transparent text-muted"}`}>{status}</span>;
}

export function VerdictBadge({ verdict }: { verdict: Verdict }) {
  return <span className={`${BASE} ${VERDICT_TONE[verdict] ?? "bg-transparent text-muted"}`}>{verdict}</span>;
}
