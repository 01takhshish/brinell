"""The worker loop.

One rule shapes this file: **never hold a database transaction open across a provider call.**
An agent case can take thirty seconds; a Postgres transaction held that long pins a connection,
blocks `VACUUM`, and turns one slow model into a database incident. So the loop is three short
transactions with the slow part in between:

    claim (commit)  ->  execute (no transaction at all)  ->  complete (commit)

That gap is exactly where a worker can die, and it is why `complete_case` re-checks
`status = 'claimed'` before writing: by the time we come back, the lease may belong to someone
else, and their result wins. A heartbeat keeps the lease alive while we are legitimately working,
so the lease can stay short (seconds, not minutes) without punishing slow-but-healthy cases.

Concurrency is bounded by the run's own `concurrency` (clamped by `max_run_concurrency`), and the
claim batch never exceeds free capacity — a worker does not hold leases on work it is not
actually running.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import socket
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from crucible.config import settings
from crucible.db.session import session_scope
from crucible.domain.assertions import AssertionSpec, parse_assertions
from crucible.errors import NotFound
from crucible.logging import get_logger
from crucible.providers.registry import ProviderRegistry
from crucible.runner.cache import ResponseCache
from crucible.runner.executor import CaseExecutor, CaseOutcome
from crucible.runner.judge import build_judge
from crucible.runner.queue import (
    ClaimedCase,
    claim_cases,
    claimable_run_ids,
    complete_case,
    exhaust_stuck_cases,
    finalize_run,
    pending_count,
    renew_leases,
    run_status,
)
from crucible.runner.ratelimit import RateLimiter
from crucible.runner.retry import RetryPolicy

log = get_logger(__name__)

TERMINAL_RUN_STATUSES = frozenset({"succeeded", "failed", "cancelled"})


def default_worker_id() -> str:
    """Identifies the lease holder in `run_cases.claimed_by`. Host + pid + nonce, so two workers
    in the same container and a worker restarted under the same pid are still distinguishable."""
    host = re.sub(r"[^A-Za-z0-9_-]", "-", socket.gethostname())[:32]
    return f"{host}-{os.getpid()}-{uuid4().hex[:6]}"


LOAD_RUN_SQL = text(
    """
    SELECT r.id, r.project_id, r.status, r.concurrency, r.max_attempts, r.assertions,
           tv.config AS target_config, s.judge_model
    FROM runs r
    JOIN target_versions tv ON tv.id = r.target_version_id
    JOIN suites s ON s.id = r.suite_id
    WHERE r.id = :run_id
    """
)


@dataclass(frozen=True)
class RunSpec:
    run_id: UUID
    project_id: UUID
    status: str
    target_config: dict[str, Any]
    assertions: list[AssertionSpec]
    concurrency: int
    max_attempts: int
    judge_model: str | None


async def load_run_spec(session: AsyncSession, run_id: UUID) -> RunSpec | None:
    row = (await session.execute(LOAD_RUN_SQL, {"run_id": run_id})).first()
    if row is None:
        return None
    return RunSpec(
        run_id=row.id,
        project_id=row.project_id,
        status=row.status,
        target_config=row.target_config or {},
        # The run's own snapshot, not the suite's current one — editing a suite must not change
        # what a finished (or in-flight) run was measured against. Parsed once here rather than
        # per case: the specs are immutable for the life of the run, and a snapshot that no longer
        # validates means someone edited `runs.assertions` by hand, which should fail loudly.
        assertions=parse_assertions(row.assertions or []),
        concurrency=max(1, min(int(row.concurrency), settings.max_run_concurrency)),
        max_attempts=int(row.max_attempts),
        judge_model=row.judge_model,
    )


@dataclass
class DrainReport:
    run_id: UUID
    status: str
    claimed: int = 0
    completed: int = 0
    discarded: int = 0
    exhausted: int = 0
    finalized: dict[str, Any] | None = None
    cache_hits: int = 0
    notes: list[str] = field(default_factory=list)


class Worker:
    """Drains one run at a time. Many instances may drain the same run concurrently — the queue's
    `SKIP LOCKED` claim and conditional completion are what make that safe."""

    def __init__(
        self,
        *,
        registry: ProviderRegistry | None = None,
        redis: Redis | None = None,
        worker_id: str | None = None,
        poll_interval_ms: int | None = None,
        batch_size: int | None = None,
        lease_seconds: int | None = None,
        retry_policy: RetryPolicy | None = None,
        max_idle_polls: int = 20,
    ) -> None:
        self.worker_id = worker_id or default_worker_id()
        self.registry = registry or ProviderRegistry()
        self._redis = redis
        self._poll_s = (poll_interval_ms or settings.worker_poll_interval_ms) / 1000
        self._batch_size = batch_size or settings.worker_batch_size
        self._lease_seconds = lease_seconds or settings.case_lease_seconds
        self._retry_policy = retry_policy or RetryPolicy(max_attempts=3)
        # How many consecutive empty polls we tolerate while *other* workers hold live leases
        # before concluding this worker has nothing left to contribute to the run.
        self._max_idle_polls = max_idle_polls
        self._cache = ResponseCache(self._redis)
        self._limiter = RateLimiter(self._redis)

    # ── execution ────────────────────────────────────────────────────────────

    def _executor_for(self, spec: RunSpec) -> CaseExecutor:
        judge = build_judge(self.registry, spec.judge_model) if _needs_judge(spec.assertions) else None
        return CaseExecutor(
            self.registry,
            cache=self._cache,
            limiter=self._limiter,
            retry_policy=self._retry_policy,
            judge=judge,
        )

    async def _execute(self, executor: CaseExecutor, spec: RunSpec, claimed: ClaimedCase) -> CaseOutcome:
        try:
            return await executor.execute(
                target_config=spec.target_config,
                case_input=claimed.input,
                expected=claimed.expected,
                assertions=spec.assertions,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # a bug in one case must not take down the worker
            log.exception("case_crashed", run_case_id=str(claimed.run_case_id), external_id=claimed.external_id)
            return CaseOutcome(
                status="error",
                output=None,
                verdict=None,
                spans=[],
                error=f"{type(exc).__name__}: {exc}"[:2000],
            )

    async def _process(self, executor: CaseExecutor, spec: RunSpec, claimed: ClaimedCase) -> bool:
        outcome = await self._execute(executor, spec, claimed)
        # New transaction: the one we claimed in is long since committed.
        async with session_scope() as session:
            persisted = await complete_case(
                session,
                run_id=spec.run_id,
                project_id=spec.project_id,
                claimed=claimed,
                outcome=outcome,
                max_attempts=spec.max_attempts,
            )
        log.info(
            "case_finished",
            run_case_id=str(claimed.run_case_id),
            external_id=claimed.external_id,
            attempt=claimed.attempt,
            status=outcome.status,
            persisted=persisted,
            cost_micros=outcome.cost_micros,
            cache_hit=outcome.cache_hit,
        )
        return persisted

    # ── lease heartbeat ──────────────────────────────────────────────────────

    async def _heartbeat(self, spec: RunSpec, inflight: dict[UUID, ClaimedCase]) -> None:
        interval = max(1.0, self._lease_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            ids = list(inflight)
            if not ids:
                continue
            try:
                async with session_scope() as session:
                    renewed = await renew_leases(
                        session,
                        run_id=spec.run_id,
                        worker_id=self.worker_id,
                        run_case_ids=ids,
                        lease_seconds=self._lease_seconds,
                    )
                if renewed < len(ids):
                    # Someone re-claimed work we still think we own. Harmless — their result wins
                    # at completion time — but worth seeing in logs when a run looks slow.
                    log.warning("leases_lost", run_id=str(spec.run_id), held=len(ids), renewed=renewed)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("heartbeat_failed", run_id=str(spec.run_id), error=str(exc))

    # ── the loop ─────────────────────────────────────────────────────────────

    async def drain(self, run_id: UUID) -> DrainReport:
        async with session_scope() as session:
            spec = await load_run_spec(session, run_id)
        if spec is None:
            raise NotFound(f"run {run_id} not found")

        report = DrainReport(run_id=run_id, status=spec.status)
        if spec.status in TERMINAL_RUN_STATUSES:
            report.notes.append(f"run already {spec.status}")
            return report

        executor = self._executor_for(spec)
        inflight: dict[UUID, ClaimedCase] = {}
        tasks: set[asyncio.Task[bool]] = set()
        heartbeat = asyncio.create_task(self._heartbeat(spec, inflight))
        idle_polls = 0
        cancelled = False

        log.info(
            "drain_started",
            run_id=str(run_id),
            worker_id=self.worker_id,
            concurrency=spec.concurrency,
            max_attempts=spec.max_attempts,
        )
        try:
            while True:
                capacity = spec.concurrency - len(tasks)
                claimed: list[ClaimedCase] = []

                if capacity > 0:
                    async with session_scope() as session:
                        status = await run_status(session, run_id)
                        if status == "cancelled":
                            cancelled = True
                        elif status in TERMINAL_RUN_STATUSES:
                            report.notes.append(f"run became {status} mid-drain")
                            break
                        else:
                            claimed = await claim_cases(
                                session,
                                run_id=run_id,
                                worker_id=self.worker_id,
                                batch_size=min(self._batch_size, capacity),
                                lease_seconds=self._lease_seconds,
                                max_attempts=spec.max_attempts,
                            )
                    if cancelled:
                        break

                for case in claimed:
                    report.claimed += 1
                    inflight[case.run_case_id] = case
                    task = asyncio.create_task(self._settle(executor, spec, case, inflight, report))
                    tasks.add(task)
                    task.add_done_callback(tasks.discard)

                if tasks:
                    # Wake as soon as a slot frees, so claiming keeps pace with completion.
                    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED, timeout=self._poll_s)
                    idle_polls = 0
                    continue

                if claimed:
                    continue

                # Nothing running, nothing claimable.
                async with session_scope() as session:
                    remaining = await pending_count(session, run_id)
                if remaining == 0:
                    break

                async with session_scope() as session:
                    exhausted = await exhaust_stuck_cases(session, run_id, spec.max_attempts)
                if exhausted:
                    report.exhausted += exhausted
                    continue

                # Live leases held by other workers. Wait for them rather than busy-looping,
                # and give up eventually so a wedged peer cannot pin this worker forever.
                idle_polls += 1
                if idle_polls >= self._max_idle_polls:
                    report.notes.append(f"{remaining} case(s) held by other workers; leaving them to it")
                    break
                await asyncio.sleep(self._poll_s)
        finally:
            heartbeat.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.gather(heartbeat, return_exceptions=True)

        report.cache_hits = self._cache.stats.hits
        await self._maybe_finalize(spec, report, cancelled=cancelled)
        log.info(
            "drain_finished",
            run_id=str(run_id),
            status=report.status,
            claimed=report.claimed,
            completed=report.completed,
            discarded=report.discarded,
        )
        return report

    async def _settle(
        self,
        executor: CaseExecutor,
        spec: RunSpec,
        case: ClaimedCase,
        inflight: dict[UUID, ClaimedCase],
        report: DrainReport,
    ) -> bool:
        try:
            persisted = await self._process(executor, spec, case)
        finally:
            inflight.pop(case.run_case_id, None)
        if persisted:
            report.completed += 1
        else:
            report.discarded += 1
        return persisted

    async def _maybe_finalize(self, spec: RunSpec, report: DrainReport, *, cancelled: bool) -> None:
        """Only the worker that observes an empty queue closes the run.

        `finalize_run` is itself conditional on the run still being `queued`/`running`, so if two
        workers reach this line together exactly one write lands.
        """
        async with session_scope() as session:
            if cancelled:
                report.finalized = await finalize_run(session, spec.run_id, force_status="cancelled")
            else:
                report.exhausted += await exhaust_stuck_cases(session, spec.run_id, spec.max_attempts)
                if await pending_count(session, spec.run_id) == 0:
                    report.finalized = await finalize_run(session, spec.run_id)
            status = await run_status(session, spec.run_id)
        report.status = status or report.status

    # ── standalone process ───────────────────────────────────────────────────

    async def serve(self, *, stop: asyncio.Event | None = None, idle_sleep_s: float = 2.0) -> None:
        """Poll for unfinished runs and drain them. This is `crucible worker`.

        Runs are also drained inline by the API for interactive use; both paths are the same code,
        so a run started in the UI finishes even if no worker process is deployed.
        """
        stop = stop or asyncio.Event()
        log.info("worker_serving", worker_id=self.worker_id)
        while not stop.is_set():
            try:
                async with session_scope() as session:
                    run_ids = await claimable_run_ids(session)
            except Exception as exc:
                log.warning("worker_poll_failed", error=str(exc))
                run_ids = []

            if not run_ids:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=idle_sleep_s)
                continue

            for run_id in run_ids:
                if stop.is_set():
                    break
                try:
                    await self.drain(run_id)
                except Exception:
                    log.exception("drain_failed", run_id=str(run_id))
        log.info("worker_stopped", worker_id=self.worker_id)

    async def aclose(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()


def _needs_judge(assertions: list[AssertionSpec]) -> bool:
    """Building a judge instantiates a provider client, so only do it when a suite actually grades
    with a model."""
    return any(a.type in ("llm_judge", "rubric") for a in assertions)


def build_worker(**kwargs: Any) -> Worker:
    redis = kwargs.pop("redis", None)
    if redis is None and settings.redis_url:
        redis = Redis.from_url(settings.redis_url, decode_responses=False)
    return Worker(redis=redis, **kwargs)
