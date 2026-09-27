"""The queue's three invariants, checked against a real Postgres.

These are the tests the rest of the platform rests on. A run's numbers are only trustworthy if a
case can be counted exactly once no matter how badly a worker behaves — dying mid-case, coming back
after its lease was reassigned, or racing another worker for the same row.

Everything here drives `crucible.runner.queue` directly rather than going through the worker: the
worker is one caller of these primitives, and the primitives are where the guarantees live.
"""

from __future__ import annotations

import asyncio

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from crucible.domain.assertions import AssertionOutcome, CaseVerdict
from crucible.runner.executor import CaseOutcome
from crucible.runner.queue import (
    claim_cases,
    complete_case,
    exhaust_stuck_cases,
    finalize_run,
    pending_count,
    renew_leases,
)
from crucible.runner.service import CreateRunRequest, create_run
from tests.integration.factories import create_fixture, default_cases


def passed_outcome(text: str = "ok") -> CaseOutcome:
    verdict = CaseVerdict(
        passed=True,
        score=1.0,
        outcomes=[AssertionOutcome(type="equals", passed=True, score=1.0, weight=1.0, detail="equal")],
    )
    return CaseOutcome(status="passed", output={"text": text}, verdict=verdict, spans=[], latency_ms=12, cost_micros=25)


def error_outcome(message: str = "provider exploded") -> CaseOutcome:
    return CaseOutcome(status="error", output=None, verdict=None, spans=[], error=message)


async def make_run(db: AsyncSession, *, slug: str, n_cases: int = 6, max_attempts: int = 3):
    fx = await create_fixture(db, slug=slug, cases=default_cases(n_cases))
    created = await create_run(
        db,
        CreateRunRequest(
            project_id=fx.project_id,
            target_version_id=fx.target_version_id,
            suite_id=fx.suite_id,
            max_attempts=max_attempts,
        ),
    )
    await db.commit()
    return fx, created


async def counters(db: AsyncSession, run_id) -> dict:
    row = (
        await db.execute(
            sa.text("SELECT completed_cases, passed_cases, errored_cases, total_cost_micros FROM runs WHERE id = :r"),
            {"r": run_id},
        )
    ).one()
    return dict(row._mapping)


async def test_two_workers_never_claim_the_same_case(db: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]):
    """`FOR UPDATE SKIP LOCKED` is the whole mechanism — if it were a plain SELECT, both workers
    would read the same rows and the run would be executed (and billed) twice."""
    _, created = await make_run(db, slug="race", n_cases=40)

    async def grab(worker_id: str) -> list:
        async with sessionmaker() as session:
            claimed = await claim_cases(
                session, run_id=created.run_id, worker_id=worker_id, batch_size=20, lease_seconds=30, max_attempts=3
            )
            await session.commit()
            return claimed

    batches = await asyncio.gather(*(grab(f"worker-{i}") for i in range(4)))

    ids = [c.run_case_id for batch in batches for c in batch]
    assert len(ids) == 40, "every case should be claimed exactly once"
    assert len(set(ids)) == 40, "no case may be handed to two workers"

    # `pending_count` counts unfinished work, and a claimed case is still unfinished — what must be
    # zero here is the *claimable* remainder.
    assert await pending_count(db, created.run_id) == 40
    async with sessionmaker() as session:
        assert (
            await claim_cases(
                session, run_id=created.run_id, worker_id="late", batch_size=5, lease_seconds=30, max_attempts=3
            )
            == []
        )


async def test_a_zombie_worker_discards_its_own_result(
    db: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
):
    """Worker A claims a case and stalls. Its lease expires, B re-claims and finishes the case.
    A then wakes up and tries to write. Its write must be discarded, and the run counted once.
    """
    fx, created = await make_run(db, slug="zombie", n_cases=1)

    async with sessionmaker() as s:
        [a_claim] = await claim_cases(
            session=s, run_id=created.run_id, worker_id="worker-a", batch_size=1, lease_seconds=30, max_attempts=3
        )
        await s.commit()

    # Simulate the lease expiring rather than waiting out a real one.
    await db.execute(
        sa.text("UPDATE run_cases SET lease_expires_at = now() - interval '1 second' WHERE id = :id"),
        {"id": a_claim.run_case_id},
    )
    await db.commit()

    async with sessionmaker() as s:
        [b_claim] = await claim_cases(
            session=s, run_id=created.run_id, worker_id="worker-b", batch_size=1, lease_seconds=30, max_attempts=3
        )
        await s.commit()
    assert b_claim.run_case_id == a_claim.run_case_id
    assert b_claim.attempt == a_claim.attempt + 1, "a reclaim is a new attempt"

    async with sessionmaker() as s:
        assert (
            await complete_case(
                session=s,
                run_id=created.run_id,
                project_id=fx.project_id,
                claimed=b_claim,
                outcome=passed_outcome("b wins"),
                max_attempts=3,
            )
            is True
        )
        await s.commit()

    async with sessionmaker() as s:
        accepted = await complete_case(
            session=s,
            run_id=created.run_id,
            project_id=fx.project_id,
            claimed=a_claim,
            outcome=passed_outcome("a is late"),
            max_attempts=3,
        )
        await s.commit()
    assert accepted is False, "the stale claim must be discarded, not overwrite a newer result"

    assert await counters(db, created.run_id) == {
        "completed_cases": 1,
        "passed_cases": 1,
        "errored_cases": 0,
        "total_cost_micros": 25,
    }
    output = (
        await db.execute(sa.text("SELECT output FROM run_cases WHERE id = :id"), {"id": a_claim.run_case_id})
    ).scalar_one()
    assert output["text"] == "b wins"


async def test_completing_the_same_claim_twice_counts_once(
    db: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
):
    """A worker that crashes between writing its result and acking — then retries on restart."""
    fx, created = await make_run(db, slug="twice", n_cases=1)

    async with sessionmaker() as s:
        [claim] = await claim_cases(
            session=s, run_id=created.run_id, worker_id="w", batch_size=1, lease_seconds=30, max_attempts=3
        )
        await s.commit()

    for expected in (True, False):
        async with sessionmaker() as s:
            got = await complete_case(
                session=s,
                run_id=created.run_id,
                project_id=fx.project_id,
                claimed=claim,
                outcome=passed_outcome(),
                max_attempts=3,
            )
            await s.commit()
        assert got is expected

    assert (await counters(db, created.run_id))["completed_cases"] == 1


async def test_a_transient_error_returns_the_case_to_the_queue(
    db: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
):
    """An errored case with attempts left is requeued, not recorded — a provider blip should cost
    a retry, not a red run. On the last attempt it becomes a real, counted error."""
    fx, created = await make_run(db, slug="retry", n_cases=1, max_attempts=2)

    async with sessionmaker() as s:
        [first] = await claim_cases(
            session=s, run_id=created.run_id, worker_id="w1", batch_size=1, lease_seconds=30, max_attempts=2
        )
        assert (
            await complete_case(
                session=s,
                run_id=created.run_id,
                project_id=fx.project_id,
                claimed=first,
                outcome=error_outcome("503 from upstream"),
                max_attempts=2,
            )
            is True
        )
        await s.commit()

    assert await pending_count(db, created.run_id) == 1, "attempt 1 of 2 failed: try again"
    assert (await counters(db, created.run_id))["errored_cases"] == 0

    async with sessionmaker() as s:
        [second] = await claim_cases(
            session=s, run_id=created.run_id, worker_id="w2", batch_size=1, lease_seconds=30, max_attempts=2
        )
        await complete_case(
            session=s,
            run_id=created.run_id,
            project_id=fx.project_id,
            claimed=second,
            outcome=error_outcome("503 again"),
            max_attempts=2,
        )
        await s.commit()

    assert await pending_count(db, created.run_id) == 0
    assert (await counters(db, created.run_id))["errored_cases"] == 1
    status, error = (
        await db.execute(sa.text("SELECT status, error FROM run_cases WHERE run_id = :r"), {"r": created.run_id})
    ).one()
    assert status == "error" and "503 again" in error


async def test_exhausted_cases_do_not_wedge_a_run(db: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]):
    """A worker that dies for good leaves a claimed row behind. Once its lease has expired and it
    is out of attempts, the run must be able to finish rather than wait forever."""
    fx, created = await make_run(db, slug="wedged", n_cases=2, max_attempts=1)

    async with sessionmaker() as s:
        claims = await claim_cases(
            session=s, run_id=created.run_id, worker_id="doomed", batch_size=2, lease_seconds=30, max_attempts=1
        )
        await complete_case(
            session=s,
            run_id=created.run_id,
            project_id=fx.project_id,
            claimed=claims[0],
            outcome=passed_outcome(),
            max_attempts=1,
        )
        await s.commit()

    await db.execute(
        sa.text("UPDATE run_cases SET lease_expires_at = now() - interval '1 second' WHERE status = 'claimed'")
    )
    await db.commit()

    assert await pending_count(db, created.run_id) == 1, "the abandoned case still counts as unfinished"
    assert await exhaust_stuck_cases(db, created.run_id, max_attempts=1) == 1
    await db.commit()
    assert await pending_count(db, created.run_id) == 0

    result = await finalize_run(db, created.run_id)
    await db.commit()
    assert result is not None and result["status"] == "failed"
    assert (await counters(db, created.run_id)) == {
        "completed_cases": 2,
        "passed_cases": 1,
        "errored_cases": 1,
        "total_cost_micros": 25,
    }


async def test_finalize_is_idempotent_under_concurrent_workers(
    db: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
):
    """Every worker calls `finalize_run` when it sees the queue drain. Exactly one write may land,
    or a second worker would stamp a new `finished_at` over the real one."""
    fx, created = await make_run(db, slug="finalrace", n_cases=2)

    async with sessionmaker() as s:
        for claim in await claim_cases(
            session=s, run_id=created.run_id, worker_id="w", batch_size=2, lease_seconds=30, max_attempts=3
        ):
            await complete_case(
                session=s,
                run_id=created.run_id,
                project_id=fx.project_id,
                claimed=claim,
                outcome=passed_outcome(),
                max_attempts=3,
            )
        await s.commit()

    async def finalize() -> dict | None:
        async with sessionmaker() as session:
            result = await finalize_run(session, created.run_id)
            await session.commit()
            return result

    results = await asyncio.gather(*(finalize() for _ in range(4)))
    assert sum(1 for r in results if r is not None) == 1, "only one worker may finalize a run"

    status = (await db.execute(sa.text("SELECT status FROM runs WHERE id = :r"), {"r": created.run_id})).scalar_one()
    assert status == "succeeded"


async def test_renewing_a_lease_keeps_a_slow_case_safe(
    db: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
):
    """The heartbeat is what lets leases be short. Without it, a case slower than the lease would be
    stolen from a perfectly healthy worker."""
    _, created = await make_run(db, slug="slow", n_cases=1)

    async with sessionmaker() as s:
        [claim] = await claim_cases(
            session=s, run_id=created.run_id, worker_id="slow-worker", batch_size=1, lease_seconds=1, max_attempts=3
        )
        await s.commit()

    await asyncio.sleep(1.2)  # the original lease is now expired
    async with sessionmaker() as s:
        renewed = await renew_leases(
            s, run_id=created.run_id, worker_id="slow-worker", run_case_ids=[claim.run_case_id], lease_seconds=60
        )
        await s.commit()
    assert renewed == 1

    async with sessionmaker() as s:
        stolen = await claim_cases(
            session=s, run_id=created.run_id, worker_id="thief", batch_size=1, lease_seconds=30, max_attempts=3
        )
        await s.commit()
    assert stolen == [], "a renewed lease must not be claimable"


async def test_renewal_by_the_wrong_worker_does_nothing(
    db: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
):
    """A zombie must not be able to extend a lease it no longer holds."""
    _, created = await make_run(db, slug="wrongworker", n_cases=1)

    async with sessionmaker() as s:
        [claim] = await claim_cases(
            session=s, run_id=created.run_id, worker_id="holder", batch_size=1, lease_seconds=30, max_attempts=3
        )
        await s.commit()

    async with sessionmaker() as s:
        renewed = await renew_leases(
            s, run_id=created.run_id, worker_id="impostor", run_case_ids=[claim.run_case_id], lease_seconds=600
        )
        await s.commit()
    assert renewed == 0
