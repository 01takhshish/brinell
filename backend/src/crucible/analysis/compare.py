"""Run-to-run comparison.

The number nobody can act on is "pass rate went from 94% to 91%". The number that ships or blocks
a PR is "these four cases broke, these two were fixed, and the mean score on `refund-*` dropped".
So the comparison is per-case and keyed on `external_id` — the identity that survives across
dataset versions — rather than on row ids or ordinal position.

Cases only in one side are reported as `added`/`removed` rather than being silently dropped: a
dataset edit that deletes the failing cases would otherwise read as a fix.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from crucible.errors import NotFound

Verdict = Literal["broken", "fixed", "regressed", "improved", "unchanged", "added", "removed", "flaky"]

# Score moves smaller than this are noise from judge sampling, not signal.
SCORE_EPSILON = 0.02

CASES_SQL = text(
    """
    SELECT rc.external_id, rc.status, rc.score, rc.passed, rc.cost_micros, rc.latency_ms,
           rc.error, rc.attempt, rc.assertion_results
    FROM run_cases rc
    WHERE rc.run_id = :run_id
    """
)

RUN_SQL = text(
    """
    SELECT r.id, r.project_id, r.status, r.pass_rate, r.mean_score, r.total_cost_micros,
           r.p50_latency_ms, r.p95_latency_ms, r.total_cases, r.completed_cases, r.label,
           r.git_sha, r.created_at, tv.semver AS target_semver, tv.content_hash AS target_hash,
           t.name AS target_name, s.name AS suite_name
    FROM runs r
    JOIN target_versions tv ON tv.id = r.target_version_id
    JOIN targets t ON t.id = tv.target_id
    JOIN suites s ON s.id = r.suite_id
    WHERE r.id = :run_id AND r.project_id = :project_id
    """
)


@dataclass
class CaseDelta:
    external_id: str
    verdict: Verdict
    baseline_status: str | None
    current_status: str | None
    baseline_score: float | None
    current_score: float | None
    score_delta: float | None
    cost_delta_micros: int | None
    latency_delta_ms: int | None
    detail: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ComparisonSummary:
    broken: int = 0
    fixed: int = 0
    regressed: int = 0
    improved: int = 0
    unchanged: int = 0
    added: int = 0
    removed: int = 0

    def count(self, verdict: Verdict) -> None:
        setattr(self, verdict, getattr(self, verdict) + 1)


@dataclass
class Comparison:
    baseline_run_id: UUID
    current_run_id: UUID
    baseline: dict[str, Any]
    current: dict[str, Any]
    summary: ComparisonSummary
    deltas: list[CaseDelta] = field(default_factory=list)

    @property
    def has_regressions(self) -> bool:
        """What `--fail-on-regression` keys off. A score dip alone is a warning; a case that used
        to pass and now does not is a regression."""
        return self.summary.broken > 0

    @property
    def pass_rate_delta(self) -> float | None:
        b, c = self.baseline.get("pass_rate"), self.current.get("pass_rate")
        return None if b is None or c is None else round(c - b, 6)

    @property
    def cost_delta_micros(self) -> int:
        return int(self.current.get("total_cost_micros") or 0) - int(self.baseline.get("total_cost_micros") or 0)

    def as_dict(self) -> dict[str, Any]:
        return {
            "baseline_run_id": str(self.baseline_run_id),
            "current_run_id": str(self.current_run_id),
            "baseline": _stringify_run(self.baseline),
            "current": _stringify_run(self.current),
            "summary": asdict(self.summary),
            "pass_rate_delta": self.pass_rate_delta,
            "cost_delta_micros": self.cost_delta_micros,
            "has_regressions": self.has_regressions,
            "deltas": [d.as_dict() for d in self.deltas],
        }


async def compare_runs(
    session: AsyncSession, *, project_id: UUID, baseline_run_id: UUID, current_run_id: UUID
) -> Comparison:
    baseline_run = await _load_run(session, project_id, baseline_run_id)
    current_run = await _load_run(session, project_id, current_run_id)

    baseline_cases = {r.external_id: r for r in (await session.execute(CASES_SQL, {"run_id": baseline_run_id})).all()}
    current_cases = {r.external_id: r for r in (await session.execute(CASES_SQL, {"run_id": current_run_id})).all()}

    summary = ComparisonSummary()
    deltas: list[CaseDelta] = []

    for external_id in sorted(set(baseline_cases) | set(current_cases)):
        before = baseline_cases.get(external_id)
        after = current_cases.get(external_id)
        delta = _classify(external_id, before, after)
        summary.count(delta.verdict)
        deltas.append(delta)

    # Most actionable first: breakages, then score regressions, then everything else.
    order = {"broken": 0, "regressed": 1, "removed": 2, "added": 3, "fixed": 4, "improved": 5, "unchanged": 6}
    deltas.sort(key=lambda d: (order.get(d.verdict, 9), -abs(d.score_delta or 0.0), d.external_id))

    return Comparison(
        baseline_run_id=baseline_run_id,
        current_run_id=current_run_id,
        baseline=baseline_run,
        current=current_run,
        summary=summary,
        deltas=deltas,
    )


def _classify(external_id: str, before: Any, after: Any) -> CaseDelta:
    if before is None:
        return CaseDelta(
            external_id=external_id,
            verdict="added",
            baseline_status=None,
            current_status=after.status,
            baseline_score=None,
            current_score=after.score,
            score_delta=None,
            cost_delta_micros=None,
            latency_delta_ms=None,
            detail="not present in the baseline run",
        )
    if after is None:
        return CaseDelta(
            external_id=external_id,
            verdict="removed",
            baseline_status=before.status,
            current_status=None,
            baseline_score=before.score,
            current_score=None,
            score_delta=None,
            cost_delta_micros=None,
            latency_delta_ms=None,
            detail="dropped from the dataset since the baseline",
        )

    score_delta = None
    if before.score is not None and after.score is not None:
        score_delta = round(float(after.score) - float(before.score), 6)

    was_ok = before.status == "passed"
    is_ok = after.status == "passed"

    if was_ok and not is_ok:
        verdict: Verdict = "broken"
        detail = _failure_detail(after)
    elif not was_ok and is_ok:
        verdict, detail = "fixed", None
    elif score_delta is not None and score_delta < -SCORE_EPSILON:
        verdict, detail = "regressed", f"score fell {abs(score_delta):.3f} without changing pass/fail"
    elif score_delta is not None and score_delta > SCORE_EPSILON:
        verdict, detail = "improved", None
    else:
        verdict, detail = "unchanged", None

    return CaseDelta(
        external_id=external_id,
        verdict=verdict,
        baseline_status=before.status,
        current_status=after.status,
        baseline_score=before.score,
        current_score=after.score,
        score_delta=score_delta,
        cost_delta_micros=int(after.cost_micros or 0) - int(before.cost_micros or 0),
        latency_delta_ms=(
            None if after.latency_ms is None or before.latency_ms is None else after.latency_ms - before.latency_ms
        ),
        detail=detail,
    )


def _failure_detail(row: Any) -> str:
    """Name the assertion that broke, not just "failed" — the whole point of the diff."""
    if row.error:
        return str(row.error)[:300]
    for outcome in row.assertion_results or []:
        if not outcome.get("passed"):
            return f"{outcome.get('type')}: {outcome.get('detail')}"[:300]
    return "failed"


async def _load_run(session: AsyncSession, project_id: UUID, run_id: UUID) -> dict[str, Any]:
    row = (await session.execute(RUN_SQL, {"run_id": run_id, "project_id": project_id})).first()
    if row is None:
        raise NotFound(f"run {run_id} not found in this project")
    return dict(row._mapping)


def _stringify_run(run: dict[str, Any]) -> dict[str, Any]:
    out = dict(run)
    for key in ("id", "project_id"):
        if key in out and out[key] is not None:
            out[key] = str(out[key])
    if out.get("created_at") is not None:
        out["created_at"] = out["created_at"].isoformat()
    return out
