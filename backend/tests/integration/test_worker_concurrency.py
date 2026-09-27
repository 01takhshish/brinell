"""Two workers, one run.

The queue primitives are proven in `test_queue_idempotency`; this checks that the worker loop uses
them correctly — that two processes racing over the same run split the work instead of duplicating
it, and that the run's counters still add up afterwards.
"""

from __future__ import annotations

import asyncio

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from crucible.providers.mock import MockBehaviour, MockProvider
from crucible.providers.registry import ProviderRegistry
from crucible.runner.service import CreateRunRequest, create_run
from crucible.runner.worker import Worker
from tests.integration.factories import create_fixture, default_cases


def make_worker(**behaviour) -> Worker:
    registry = ProviderRegistry()
    registry.register(MockProvider(MockBehaviour(**behaviour)))
    return Worker(registry=registry, redis=None, lease_seconds=30, batch_size=4)


async def test_two_workers_split_a_run_without_double_counting(db: AsyncSession, patched_session_scope: None):
    fx = await create_fixture(db, slug="split", cases=default_cases(24))
    created = await create_run(
        db,
        CreateRunRequest(
            project_id=fx.project_id, target_version_id=fx.target_version_id, suite_id=fx.suite_id, concurrency=4
        ),
    )
    await db.commit()

    reports = await asyncio.gather(*(make_worker(latency_ms=5).drain(created.run_id) for _ in range(3)))

    assert sum(r.claimed for r in reports) == 24, "each case claimed exactly once across all workers"
    assert sum(r.completed for r in reports) == 24
    assert sum(r.discarded for r in reports) == 0
    assert sum(1 for r in reports if r.finalized) == 1, "exactly one worker finalizes"

    run = (await db.execute(sa.text("SELECT * FROM runs WHERE id = :r"), {"r": created.run_id})).one()
    assert (run.completed_cases, run.passed_cases, run.errored_cases) == (24, 24, 0)
    assert run.status == "succeeded"

    distinct_workers = (
        await db.execute(
            sa.text("SELECT count(DISTINCT claimed_by) FROM run_cases WHERE run_id = :r"), {"r": created.run_id}
        )
    ).scalar_one()
    assert distinct_workers > 1, "the point of the test is that the work actually was shared"


async def test_a_flaky_provider_costs_retries_not_the_run(db: AsyncSession, patched_session_scope: None):
    """Half of all calls fail transiently. With enough attempts the run should still come out
    green — and deterministically so: the mock draws its failures per (request, attempt), so this
    is a fixed sequence rather than a coin flip that will flake in CI."""
    fx = await create_fixture(db, slug="flaky", cases=default_cases(8))
    created = await create_run(
        db,
        CreateRunRequest(
            project_id=fx.project_id, target_version_id=fx.target_version_id, suite_id=fx.suite_id, max_attempts=6
        ),
    )
    await db.commit()

    await make_worker(failure_rate=0.5).drain(created.run_id)

    run = (await db.execute(sa.text("SELECT * FROM runs WHERE id = :r"), {"r": created.run_id})).one()
    assert run.completed_cases == 8
    assert run.status == "succeeded", "transient provider failures must not turn into a red run"
