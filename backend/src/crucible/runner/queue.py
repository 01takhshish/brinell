"""The work queue.

`run_cases` *is* the queue. A worker claims rows with `FOR UPDATE SKIP LOCKED`, executes them, and
writes the result — and because the queue and the results live in the same database, claiming and
completing share one transaction. That is what makes a worker crash mid-suite survivable without
an external broker's delivery semantics to reason about (docs/adr/0001).

Three invariants everything else depends on:

1. **A case has at most one result.** `UNIQUE (run_id, case_id)` plus a conditional
   `UPDATE ... WHERE status = 'claimed'` means a worker that comes back from the dead after its
   lease was reassigned discards its own result instead of overwriting a newer one.
2. **Run counters are incremented exactly once per case.** The same conditional update is the
   guard: only the transition that actually moves a case to a terminal state gets to count it.
3. **A lost worker costs one lease, not the run.** Leases expire; expired cases are re-claimable
   and their attempt counter advances until `max_attempts`.
"""

import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from crucible.logging import get_logger
from crucible.runner.executor import CaseOutcome

log = get_logger(__name__)


@dataclass
class ClaimedCase:
    run_case_id: UUID
    case_id: UUID
    external_id: str
    attempt: int
    input: dict[str, Any]
    expected: dict[str, Any] | None


CLAIM_SQL = text(
    """
    WITH claimable AS (
        SELECT rc.id
        FROM run_cases rc
        WHERE rc.run_id = :run_id
          AND rc.status IN ('pending', 'claimed')
          AND (rc.lease_expires_at IS NULL OR rc.lease_expires_at < now())
          AND rc.attempt < :max_attempts
        ORDER BY rc.external_id
        FOR UPDATE SKIP LOCKED
        LIMIT :batch_size
    )
    UPDATE run_cases rc
    SET status = 'claimed',
        attempt = rc.attempt + 1,
        claimed_by = :worker_id,
        lease_expires_at = now() + make_interval(secs => :lease_seconds),
        started_at = COALESCE(rc.started_at, now())
    FROM claimable c
    WHERE rc.id = c.id
    RETURNING rc.id, rc.case_id, rc.external_id, rc.attempt
    """
)

COMPLETE_SQL = text(
    """
    UPDATE run_cases
    SET status = :status,
        output = CAST(:output AS jsonb),
        score = :score,
        passed = :passed,
        assertion_results = CAST(:assertion_results AS jsonb),
        input_tokens = :input_tokens,
        output_tokens = :output_tokens,
        cost_micros = :cost_micros,
        latency_ms = :latency_ms,
        cache_hit = :cache_hit,
        error = :error,
        finished_at = now(),
        lease_expires_at = NULL
    WHERE id = :id AND status = 'claimed'
    RETURNING id
    """
)

RELEASE_FOR_RETRY_SQL = text(
    """
    UPDATE run_cases
    SET status = 'pending', lease_expires_at = NULL, error = :error, claimed_by = NULL
    WHERE id = :id AND status = 'claimed'
    RETURNING id
    """
)

BUMP_RUN_SQL = text(
    """
    UPDATE runs
    SET completed_cases = completed_cases + 1,
        passed_cases = passed_cases + :passed_delta,
        errored_cases = errored_cases + :errored_delta,
        total_cost_micros = total_cost_micros + :cost_micros,
        total_input_tokens = total_input_tokens + :input_tokens,
        total_output_tokens = total_output_tokens + :output_tokens,
        cache_hits = cache_hits + :cache_delta,
        status = CASE WHEN status = 'queued' THEN 'running' ELSE status END,
        started_at = COALESCE(started_at, now())
    WHERE id = :run_id
    """
)

FINALIZE_SQL = text(
    """
    WITH stats AS (
        SELECT
            count(*) FILTER (WHERE status = 'passed')::float AS passed,
            count(*)::float AS total,
            avg(score) AS mean_score,
            percentile_cont(0.50) WITHIN GROUP (ORDER BY latency_ms) AS p50,
            percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95,
            percentile_cont(0.99) WITHIN GROUP (ORDER BY latency_ms) AS p99
        FROM run_cases
        WHERE run_id = :run_id AND status IN ('passed', 'failed', 'error')
    )
    UPDATE runs r
    -- The cast is load-bearing: `:force_status` is NULL on the normal path, and Postgres
    -- cannot infer a type for a bare NULL parameter inside a CASE.
    SET status = CASE WHEN CAST(:force_status AS text) IS NOT NULL THEN CAST(:force_status AS text)
                      WHEN s.passed = s.total THEN 'succeeded' ELSE 'failed' END,
        pass_rate = CASE WHEN s.total > 0 THEN s.passed / s.total ELSE NULL END,
        mean_score = s.mean_score,
        p50_latency_ms = round(s.p50)::int,
        p95_latency_ms = round(s.p95)::int,
        p99_latency_ms = round(s.p99)::int,
        finished_at = now()
    FROM stats s
    WHERE r.id = :run_id AND r.status IN ('queued', 'running')
    RETURNING r.id, r.status, r.pass_rate, r.mean_score
    """
)


async def claim_cases(
    session: AsyncSession, *, run_id: UUID, worker_id: str, batch_size: int, lease_seconds: int, max_attempts: int
) -> list[ClaimedCase]:
    rows = (
        await session.execute(
            CLAIM_SQL,
            {
                "run_id": run_id,
                "worker_id": worker_id,
                "batch_size": batch_size,
                "lease_seconds": lease_seconds,
                "max_attempts": max_attempts,
            },
        )
    ).all()
    if not rows:
        return []

    case_ids = [r.case_id for r in rows]
    payloads = {
        r.id: (r.input, r.expected)
        for r in (
            await session.execute(text("SELECT id, input, expected FROM cases WHERE id = ANY(:ids)"), {"ids": case_ids})
        ).all()
    }
    return [
        ClaimedCase(
            run_case_id=r.id,
            case_id=r.case_id,
            external_id=r.external_id,
            attempt=r.attempt,
            input=payloads.get(r.case_id, ({}, None))[0] or {},
            expected=payloads.get(r.case_id, ({}, None))[1],
        )
        for r in rows
    ]


async def complete_case(
    session: AsyncSession,
    *,
    run_id: UUID,
    project_id: UUID,
    claimed: ClaimedCase,
    outcome: CaseOutcome,
    max_attempts: int,
) -> bool:
    """Persist a case result. Returns False when the claim was already reassigned.

    An errored case with attempts remaining goes back to `pending` rather than to a terminal state,
    so a transient provider outage costs a retry rather than a red run.
    """

    retryable = outcome.status == "error" and claimed.attempt < max_attempts
    if retryable:
        released = (
            await session.execute(RELEASE_FOR_RETRY_SQL, {"id": claimed.run_case_id, "error": outcome.error})
        ).first()
        if released is None:
            log.warning("stale_claim_discarded", run_case_id=str(claimed.run_case_id), reason="retry")
            return False
        return True

    updated = (
        await session.execute(
            COMPLETE_SQL,
            {
                "id": claimed.run_case_id,
                "status": outcome.status,
                "output": json.dumps(outcome.output) if outcome.output is not None else None,
                "score": outcome.verdict.score if outcome.verdict else None,
                "passed": outcome.verdict.passed if outcome.verdict else None,
                "assertion_results": json.dumps(outcome.verdict.to_list()) if outcome.verdict else None,
                "input_tokens": outcome.input_tokens,
                "output_tokens": outcome.output_tokens,
                "cost_micros": outcome.cost_micros,
                "latency_ms": outcome.latency_ms,
                "cache_hit": outcome.cache_hit,
                "error": outcome.error,
            },
        )
    ).first()

    if updated is None:
        # Our lease was reassigned while we were working. The other worker's result is authoritative;
        # ours is dropped rather than racing it. This is the branch that prevents double-counting.
        log.warning("stale_claim_discarded", run_case_id=str(claimed.run_case_id), attempt=claimed.attempt)
        return False

    if outcome.spans:
        await session.execute(
            text(
                """
                INSERT INTO spans (id, project_id, run_id, run_case_id, attempt, seq, kind, name, provider, model,
                                   request, response, input_tokens, output_tokens, cost_micros, latency_ms,
                                   cache_hit, retries, error, started_at)
                SELECT gen_random_uuid(), :project_id, :run_id, :run_case_id, :attempt,
                       (s->>'seq')::int, s->>'kind', s->>'name', s->>'provider', s->>'model',
                       s->'request', s->'response', (s->>'input_tokens')::int, (s->>'output_tokens')::int,
                       (s->>'cost_micros')::bigint, (s->>'latency_ms')::int, (s->>'cache_hit')::boolean,
                       (s->>'retries')::int, s->>'error', now()
                FROM jsonb_array_elements(CAST(:spans AS jsonb)) AS s
                """
            ),
            {
                "project_id": project_id,
                "run_id": run_id,
                "run_case_id": claimed.run_case_id,
                "attempt": claimed.attempt,
                "spans": json.dumps([_span_json(s) for s in outcome.spans]),
            },
        )

    await session.execute(
        BUMP_RUN_SQL,
        {
            "run_id": run_id,
            "passed_delta": 1 if outcome.status == "passed" else 0,
            "errored_delta": 1 if outcome.status == "error" else 0,
            "cost_micros": outcome.cost_micros,
            "input_tokens": outcome.input_tokens,
            "output_tokens": outcome.output_tokens,
            "cache_delta": 1 if outcome.cache_hit else 0,
        },
    )
    return True


def _span_json(span: Any) -> dict[str, Any]:
    return {
        "seq": span.seq,
        "kind": span.kind,
        "name": span.name,
        "provider": span.provider,
        "model": span.model,
        "request": span.request,
        "response": span.response,
        "input_tokens": span.input_tokens,
        "output_tokens": span.output_tokens,
        "cost_micros": span.cost_micros,
        "latency_ms": span.latency_ms,
        "cache_hit": span.cache_hit,
        "retries": span.retries,
        "error": span.error,
    }


async def pending_count(session: AsyncSession, run_id: UUID) -> int:
    row = (
        await session.execute(
            text("SELECT count(*) AS n FROM run_cases WHERE run_id = :run_id AND status IN ('pending','claimed')"),
            {"run_id": run_id},
        )
    ).first()
    return int(row.n) if row else 0


async def exhaust_stuck_cases(session: AsyncSession, run_id: UUID, max_attempts: int) -> int:
    """Cases that burned every attempt are terminal errors, not work to retry forever."""
    rows = (
        await session.execute(
            text(
                """
                UPDATE run_cases
                SET status = 'error', finished_at = now(), lease_expires_at = NULL,
                    error = COALESCE(error, 'exhausted all attempts')
                WHERE run_id = :run_id AND status IN ('pending','claimed') AND attempt >= :max_attempts
                RETURNING id
                """
            ),
            {"run_id": run_id, "max_attempts": max_attempts},
        )
    ).all()
    if rows:
        await session.execute(
            text(
                """
                UPDATE runs SET completed_cases = completed_cases + :n, errored_cases = errored_cases + :n
                WHERE id = :run_id
                """
            ),
            {"run_id": run_id, "n": len(rows)},
        )
    return len(rows)


async def finalize_run(session: AsyncSession, run_id: UUID, force_status: str | None = None) -> dict[str, Any] | None:
    row = (await session.execute(FINALIZE_SQL, {"run_id": run_id, "force_status": force_status})).first()
    if row is None:
        return None
    return {"id": row.id, "status": row.status, "pass_rate": row.pass_rate, "mean_score": row.mean_score}


RENEW_SQL = text(
    """
    UPDATE run_cases
    SET lease_expires_at = now() + make_interval(secs => :lease_seconds)
    WHERE run_id = :run_id AND claimed_by = :worker_id AND status = 'claimed' AND id = ANY(:ids)
    RETURNING id
    """
)


async def renew_leases(
    session: AsyncSession, *, run_id: UUID, worker_id: str, run_case_ids: list[UUID], lease_seconds: int
) -> int:
    """Extend the lease on cases this worker is still working on.

    Without this, a lease long enough for a slow agent would also be a lease long enough to stall
    a run for minutes after a worker dies. Heartbeating lets the lease stay short.
    """
    if not run_case_ids:
        return 0
    rows = (
        await session.execute(
            RENEW_SQL,
            {"run_id": run_id, "worker_id": worker_id, "ids": run_case_ids, "lease_seconds": lease_seconds},
        )
    ).all()
    return len(rows)


async def run_status(session: AsyncSession, run_id: UUID) -> str | None:
    row = (await session.execute(text("SELECT status FROM runs WHERE id = :id"), {"id": run_id})).first()
    return str(row.status) if row else None


async def claimable_run_ids(session: AsyncSession, limit: int = 20) -> list[UUID]:
    """Runs a standalone worker should pick up: unfinished, with at least one claimable case."""
    rows = (
        await session.execute(
            text(
                """
                SELECT r.id
                FROM runs r
                WHERE r.status IN ('queued', 'running')
                  AND EXISTS (
                      SELECT 1 FROM run_cases rc
                      WHERE rc.run_id = r.id
                        AND rc.status IN ('pending', 'claimed')
                        AND (rc.lease_expires_at IS NULL OR rc.lease_expires_at < now())
                        AND rc.attempt < r.max_attempts
                  )
                ORDER BY r.created_at
                LIMIT :limit
                """
            ),
            {"limit": limit},
        )
    ).all()
    return [r.id for r in rows]
