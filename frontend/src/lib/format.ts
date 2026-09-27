/**
 * Display helpers.
 *
 * `formatUsd` is a deliberate port of `crucible.domain.cost.format_usd`, not an approximation:
 * the same run must not read as $0.0011 in the CLI and $0.00 in the browser. Money is carried as
 * integer micro-dollars everywhere and only becomes a float at the moment it is printed.
 */

const MICROS_PER_USD = 1_000_000;

/** Python's `.rstrip("0").rstrip(".")`, which is what the backend applies to its formatted string. */
function trimTrailingZeros(s: string): string {
  return s.replace(/0+$/, "").replace(/\.$/, "");
}

export function formatUsd(micros: number | null | undefined): string {
  if (micros === null || micros === undefined) return "unpriced";
  const usd = micros / MICROS_PER_USD;
  if (usd === 0) return "$0.00";
  if (usd < 0.01) return "$" + trimTrailingZeros(usd.toFixed(6));
  if (usd < 1) return "$" + trimTrailingZeros(usd.toFixed(4));
  return "$" + usd.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

/** Signed, for deltas — where "no change" is worth saying out loud rather than printing $0.00. */
export function formatUsdDelta(micros: number | null | undefined): string {
  if (micros === null || micros === undefined) return "—";
  if (micros === 0) return "no change";
  const sign = micros > 0 ? "+" : "−";
  return sign + formatUsd(Math.abs(micros));
}

export function formatTokens(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  return n.toLocaleString("en-US");
}

export function formatDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "—";
  if (ms < 1000) return `${ms} ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)} s`;
  const minutes = Math.floor(ms / 60_000);
  const seconds = Math.round((ms % 60_000) / 1000);
  return `${minutes}m ${seconds}s`;
}

export function formatPercent(rate: number | null | undefined, digits = 1): string {
  if (rate === null || rate === undefined) return "—";
  return `${(rate * 100).toFixed(digits)}%`;
}

export function formatScore(score: number | null | undefined): string {
  if (score === null || score === undefined) return "—";
  return score.toFixed(3).replace(/\.?0+$/, "") || "0";
}

const RELATIVE_UNITS: [limit: number, divisor: number, unit: Intl.RelativeTimeFormatUnit][] = [
  [60, 1, "second"],
  [3600, 60, "minute"],
  [86_400, 3600, "hour"],
  [604_800, 86_400, "day"],
  [2_629_800, 604_800, "week"],
  [31_557_600, 2_629_800, "month"],
  [Infinity, 31_557_600, "year"],
];

const relative = new Intl.RelativeTimeFormat("en", { numeric: "auto" });

/**
 * Rendered on the client only. Relative time computed during a server render is stale the moment
 * it reaches the browser and, worse, makes the server and client HTML disagree.
 */
export function formatRelative(iso: string | null | undefined, now: number = Date.now()): string {
  if (!iso) return "—";
  const then = Date.parse(withUtcOffset(iso));
  if (Number.isNaN(then)) return "—";
  const seconds = (then - now) / 1000;
  const magnitude = Math.abs(seconds);
  for (const [limit, divisor, unit] of RELATIVE_UNITS) {
    if (magnitude < limit) return relative.format(Math.round(seconds / divisor), unit);
  }
  return "—";
}

export function formatAbsolute(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(withUtcOffset(iso));
  return Number.isNaN(d.getTime()) ? "—" : d.toISOString().replace("T", " ").slice(0, 19) + "Z";
}

/**
 * Postgres `timestamptz` serialises without a zone suffix through SQLAlchemy, and a bare
 * "2026-09-17T10:00:00" is parsed by JS as *local* time. On any machine not set to UTC that
 * silently shifts every timestamp in the UI by the offset, which looks like a clock bug rather
 * than a parsing one — so the zone is made explicit here instead.
 */
function withUtcOffset(iso: string): string {
  return /(?:Z|[+-]\d{2}:?\d{2})$/.test(iso) ? iso : `${iso}Z`;
}

export function shortId(id: string): string {
  return id.slice(0, 8);
}

export function shortSha(sha: string | null): string {
  return sha ? sha.slice(0, 7) : "—";
}
