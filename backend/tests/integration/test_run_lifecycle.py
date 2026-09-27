"""A run, end to end, against a real database and the deterministic mock provider.

This is the happy path plus the arithmetic that everything else reports on: counters, pass rate,
percentiles, spans, and the snapshot that makes a finished run immune to later edits.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from crucible.providers.mock import MockBehaviour, MockProvider
from crucible.providers.registry import ProviderRegistry
from crucible.runner.service import CreateRunRequest, cancel_run, create_run
from crucible.runner.worker import Worker
from tests.integration.factories import create_fixture, default_cases


def worker_with(quality: float = 1.0, **kw) -> Worker:
    registry = ProviderRegistry()
    registry.register(MockProvider(MockBehaviour(quality=quality, **kw)))
    # No Redis: the cache and limiter are both optional, and leaving them out keeps this test
    # about the queue rather than about Redis.
    return Worker(registry=registry, redis=None, lease_seconds=30, batch_size=3)


async def test_run_completes_and_counters_add_up(db: AsyncSession, patched_session_scope: None):
    fx = await create_fixture(db)
    created = await create_run(
        db,
        CreateRunRequest(project_id=fx.project_id, target_version_id=fx.target_version_id, suite_id=fx.suite_id),
    )
    await db.commit()
    assert created.total_cases == 6

    report = await worker_with(quality=1.0).drain(created.run_id)

    assert report.completed == 6
    assert report.discarded == 0

    run = (await db.execute(sa.text("SELECT * FROM runs WHERE id = :id"), {"id": created.run_id})).one()
    assert run.status == "succeeded"
    assert run.completed_cases == run.total_cases == 6
    assert run.passed_cases == 6
    assert run.errored_cases == 0
    assert run.pass_rate == 1.0
    assert run.p50_latency_ms is not None and run.p95_latency_ms is not None
    assert run.finished_at is not None


async def test_a_wrong_answer_fails_the_run_but_not_the_worker(db: AsyncSession, patched_session_scope: None):
    """Quality 0 makes every mock answer deliberately wrong: a red run, not a broken one."""
    fx = await create_fixture(db, slug="redco")
    created = await create_run(
        db,
        CreateRunRequest(project_id=fx.project_id, target_version_id=fx.target_version_id, suite_id=fx.suite_id),
    )
    await db.commit()

    await worker_with(quality=0.0).drain(created.run_id)

    run = (await db.execute(sa.text("SELECT * FROM runs WHERE id = :id"), {"id": created.run_id})).one()
    assert run.status == "failed"
    assert run.passed_cases == 0
    assert run.errored_cases == 0, "a wrong answer is a failure, not an error"
    assert run.pass_rate == 0.0


async def test_every_case_records_a_span_with_cost_and_tokens(db: AsyncSession, patched_session_scope: None):
    fx = await create_fixture(db, slug="spans")
    created = await create_run(
        db,
        CreateRunRequest(project_id=fx.project_id, target_version_id=fx.target_version_id, suite_id=fx.suite_id),
    )
    await db.commit()
    await worker_with().drain(created.run_id)

    spans = (
        await db.execute(
            sa.text("SELECT provider, model, input_tokens, output_tokens, cost_micros FROM spans WHERE run_id = :r"),
            {"r": created.run_id},
        )
    ).all()
    assert len(spans) == 6
    assert all(s.provider == "mock" for s in spans)
    assert all(s.input_tokens > 0 and s.output_tokens > 0 for s in spans)

    run_total = (
        await db.execute(sa.text("SELECT total_cost_micros FROM runs WHERE id = :r"), {"r": created.run_id})
    ).scalar_one()
    assert run_total == sum(s.cost_micros for s in spans), "run totals must equal the sum of their spans"


async def test_run_snapshots_assertions_so_editing_the_suite_cannot_rewrite_history(
    db: AsyncSession, patched_session_scope: None
):
    fx = await create_fixture(db, slug="snap")
    created = await create_run(
        db,
        CreateRunRequest(project_id=fx.project_id, target_version_id=fx.target_version_id, suite_id=fx.suite_id),
    )
    await db.commit()

    await db.execute(
        sa.text("UPDATE suites SET assertions = CAST(:a AS jsonb) WHERE id = :id"),
        {"a": '[{"type": "contains", "value": "totally different"}]', "id": fx.suite_id},
    )
    await db.commit()

    snapshot = (
        await db.execute(sa.text("SELECT assertions FROM runs WHERE id = :id"), {"id": created.run_id})
    ).scalar_one()
    assert snapshot == [{"type": "equals", "value_from": "expected.answer"}]


async def test_idempotency_key_returns_the_same_run(db: AsyncSession, patched_session_scope: None):
    """CI retries `POST /runs` after a network blip. The retry must not pay for the suite twice."""
    fx = await create_fixture(db, slug="idem")
    req = CreateRunRequest(
        project_id=fx.project_id,
        target_version_id=fx.target_version_id,
        suite_id=fx.suite_id,
        idempotency_key="ci-sha-abc123",
    )
    first = await create_run(db, req)
    await db.commit()
    second = await create_run(db, req)
    await db.commit()

    assert second.run_id == first.run_id
    assert second.reused is True
    n_runs = (await db.execute(sa.text("SELECT count(*) FROM runs"))).scalar_one()
    n_cases = (await db.execute(sa.text("SELECT count(*) FROM run_cases"))).scalar_one()
    assert n_runs == 1 and n_cases == 6


async def test_cancelling_a_run_stops_new_work(db: AsyncSession, patched_session_scope: None):
    fx = await create_fixture(db, slug="cancel", cases=default_cases(20))
    created = await create_run(
        db,
        CreateRunRequest(project_id=fx.project_id, target_version_id=fx.target_version_id, suite_id=fx.suite_id),
    )
    await db.commit()

    assert await cancel_run(db, fx.project_id, created.run_id) is True
    await db.commit()

    report = await worker_with().drain(created.run_id)
    assert report.claimed == 0

    statuses = dict(
        (
            await db.execute(
                sa.text("SELECT status, count(*) AS n FROM run_cases WHERE run_id = :r GROUP BY status"),
                {"r": created.run_id},
            )
        ).all()
    )
    assert statuses == {"skipped": 20}
    run_status = (
        await db.execute(sa.text("SELECT status FROM runs WHERE id = :r"), {"r": created.run_id})
    ).scalar_one()
    assert run_status == "cancelled"


async def test_cancelling_an_already_finished_run_is_a_no_op(db: AsyncSession, patched_session_scope: None):
    fx = await create_fixture(db, slug="latecancel")
    created = await create_run(
        db,
        CreateRunRequest(project_id=fx.project_id, target_version_id=fx.target_version_id, suite_id=fx.suite_id),
    )
    await db.commit()
    await worker_with().drain(created.run_id)

    assert await cancel_run(db, fx.project_id, created.run_id) is False
    status = (await db.execute(sa.text("SELECT status FROM runs WHERE id = :r"), {"r": created.run_id})).scalar_one()
    assert status == "succeeded", "a finished run cannot be retroactively cancelled"
