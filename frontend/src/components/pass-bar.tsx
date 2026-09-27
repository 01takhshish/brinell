import { formatPercent } from "@/lib/format";

/**
 * Pass rate as a bar plus the number.
 *
 * The bar is split three ways rather than two because "not passed" hides a distinction that
 * matters: a case that *errored* never produced a verdict, and lumping it in with genuine
 * failures makes a broken API key look like a broken agent.
 */
export function PassBar({
  passed,
  errored,
  total,
  rate,
  className = "",
}: {
  passed: number;
  errored: number;
  total: number;
  rate?: number | null;
  className?: string;
}) {
  const denominator = Math.max(total, 1);
  const passedPct = (passed / denominator) * 100;
  const erroredPct = (errored / denominator) * 100;
  const failedPct = Math.max(0, 100 - passedPct - erroredPct);
  const shown = rate ?? (total > 0 ? passed / total : null);

  return (
    <div className={`flex items-center gap-2 ${className}`}>
      <div
        className="flex h-1.5 w-24 overflow-hidden rounded-full bg-border"
        role="img"
        aria-label={`${passed} passed, ${total - passed - errored} failed, ${errored} errored, of ${total}`}
      >
        <div className="bg-pass" style={{ width: `${passedPct}%` }} />
        <div className="bg-fail" style={{ width: `${failedPct}%` }} />
        <div className="bg-warn" style={{ width: `${erroredPct}%` }} />
      </div>
      <span className="num text-sm">{formatPercent(shown)}</span>
      <span className="num text-xs text-faint">
        {passed}/{total}
      </span>
    </div>
  );
}
