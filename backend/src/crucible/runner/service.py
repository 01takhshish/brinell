"""Creating and cancelling runs.

Creating a run is the one place where "what will be measured" is frozen: the target version's
config is already immutable, and here the suite's assertions and the dataset version's cases are
snapshotted into the run. After this function commits, editing the suite or pushing a new dataset
version cannot change what this run means.

The other job is idempotency. CI retries `POST /runs` after a network blip, and a retry must not
pay for two hundred model calls twice. `idempotency_key` is unique per project (partial index on
`runs`), so a duplicate create returns the original run.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from crucible.config import settings
from crucible.domain.assertions import parse_assertions
from crucible.errors import BadRequest, Conflict, NotFound
from crucible.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class CreateRunRequest:
    project_id: UUID
    target_version_id: UUID
    suite_id: UUID
    dataset_version_id: UUID | None = None
    baseline_run_id: UUID | None = None
    concurrency: int | None = None
    max_attempts: int | None = None
    trigger: str = "api"
    git_sha: str | None = None
    git_branch: str | None = None
    label: str | None = None
    idempotency_key: str | None = None


@dataclass
class CreatedRun:
    run_id: UUID
    total_cases: int
    reused: bool
    baseline_run_id: UUID | None


async def create_run(session: AsyncSession, req: CreateRunRequest) -> CreatedRun:
    if req.idempotency_key:
        existing = (
            await session.execute(
                text(
                    """
                    SELECT id, total_cases, baseline_run_id FROM runs
                    WHERE project_id = :project_id AND idempotency_key = :key
                    """
                ),
                {"project_id": req.project_id, "key": req.idempotency_key},
            )
        ).first()
        if existing is not None:
            log.info("run_create_deduplicated", run_id=str(existing.id), key=req.idempotency_key)
            return CreatedRun(existing.id, int(existing.total_cases), True, existing.baseline_run_id)

    suite = (
        await session.execute(
            text(
                """
                SELECT id, dataset_id, assertions, judge_model FROM suites
                WHERE id = :id AND project_id = :project_id
                """
            ),
            {"id": req.suite_id, "project_id": req.project_id},
        )
    ).first()
    if suite is None:
        raise NotFound(f"suite {req.suite_id} not found in this project")

    target_version = (
        await session.execute(
            text("SELECT id, config FROM target_versions WHERE id = :id AND project_id = :project_id"),
            {"id": req.target_version_id, "project_id": req.project_id},
        )
    ).first()
    if target_version is None:
        raise NotFound(f"target version {req.target_version_id} not found in this project")

    # Fail here, not per-case at execution time: a typo'd assertion should cost zero model calls.
    try:
        parse_assertions(suite.assertions)
    except Exception as exc:
        raise BadRequest(f"suite has invalid assertions: {exc}") from exc

    dataset_version_id = req.dataset_version_id or await _latest_dataset_version(session, suite.dataset_id)
    if dataset_version_id is None:
        raise BadRequest("dataset has no versions; push cases before running")

    case_count = (
        await session.execute(
            text("SELECT count(*) AS n FROM cases WHERE dataset_version_id = :dv"),
            {"dv": dataset_version_id},
        )
    ).scalar_one()
    if not case_count:
        raise BadRequest("dataset version contains no cases")

    baseline_run_id = req.baseline_run_id
    if baseline_run_id is None:
        baseline_run_id = await _auto_baseline(session, project_id=req.project_id, suite_id=req.suite_id)

    concurrency = max(1, min(req.concurrency or settings.default_run_concurrency, settings.max_run_concurrency))
    max_attempts = max(1, req.max_attempts or settings.max_case_attempts)

    try:
        run_id = (
            await session.execute(
                text(
                    """
                    INSERT INTO runs (id, project_id, target_version_id, suite_id, dataset_version_id,
                                      baseline_run_id, status, trigger, git_sha, git_branch, label,
                                      assertions, concurrency, max_attempts, total_cases, idempotency_key)
                    VALUES (gen_random_uuid(), :project_id, :target_version_id, :suite_id, :dataset_version_id,
                            :baseline_run_id, 'queued', :trigger, :git_sha, :git_branch, :label,
                            CAST(:assertions AS jsonb), :concurrency, :max_attempts, :total_cases, :key)
                    RETURNING id
                    """
                ),
                {
                    "project_id": req.project_id,
                    "target_version_id": req.target_version_id,
                    "suite_id": req.suite_id,
                    "dataset_version_id": dataset_version_id,
                    "baseline_run_id": baseline_run_id,
                    "trigger": req.trigger,
                    "git_sha": req.git_sha,
                    "git_branch": req.git_branch,
                    "label": req.label,
                    "assertions": _json(suite.assertions),
                    "concurrency": concurrency,
                    "max_attempts": max_attempts,
                    "total_cases": case_count,
                    "key": req.idempotency_key,
                },
            )
        ).scalar_one()
    except IntegrityError as exc:
        # Two concurrent creates with the same key: one wins, the other reads the winner's row.
        raise Conflict("a run with this idempotency key is already being created") from exc

    # The whole queue is populated in one statement, so a run is never half-enqueued: either the
    # transaction commits with every case present, or there is no run at all.
    await session.execute(
        text(
            """
            INSERT INTO run_cases (id, project_id, run_id, case_id, external_id, status)
            SELECT gen_random_uuid(), :project_id, :run_id, c.id, c.external_id, 'pending'
            FROM cases c
            WHERE c.dataset_version_id = :dv
            """
        ),
        {"project_id": req.project_id, "run_id": run_id, "dv": dataset_version_id},
    )

    log.info(
        "run_created",
        run_id=str(run_id),
        suite_id=str(req.suite_id),
        target_version_id=str(req.target_version_id),
        cases=case_count,
        baseline_run_id=str(baseline_run_id) if baseline_run_id else None,
    )
    return CreatedRun(run_id, int(case_count), False, baseline_run_id)


async def cancel_run(session: AsyncSession, project_id: UUID, run_id: UUID) -> bool:
    """Stop a run. In-flight cases finish (their work is already paid for); nothing new is claimed.

    Workers notice on their next claim poll, so cancellation is cooperative rather than a kill.
    """
    row = (
        await session.execute(
            text(
                """
                UPDATE runs SET status = 'cancelled', finished_at = now()
                WHERE id = :id AND project_id = :project_id AND status IN ('queued', 'running')
                RETURNING id
                """
            ),
            {"id": run_id, "project_id": project_id},
        )
    ).first()
    if row is None:
        return False
    await session.execute(
        text(
            """
            UPDATE run_cases SET status = 'skipped', finished_at = now(), lease_expires_at = NULL
            WHERE run_id = :run_id AND status = 'pending'
            """
        ),
        {"run_id": run_id},
    )
    log.info("run_cancelled", run_id=str(run_id))
    return True


async def _latest_dataset_version(session: AsyncSession, dataset_id: UUID) -> UUID | None:
    row = (
        await session.execute(
            text("SELECT id FROM dataset_versions WHERE dataset_id = :id ORDER BY version DESC LIMIT 1"),
            {"id": dataset_id},
        )
    ).first()
    return row.id if row else None


async def _auto_baseline(session: AsyncSession, *, project_id: UUID, suite_id: UUID) -> UUID | None:
    """The most recent finished run of the same suite.

    Comparing against "the last run of this suite" is what makes a regression gate work without
    anyone having to remember a run id. An explicit baseline always wins over this.
    """
    row = (
        await session.execute(
            text(
                """
                SELECT id FROM runs
                WHERE project_id = :project_id AND suite_id = :suite_id
                  AND status IN ('succeeded', 'failed')
                ORDER BY created_at DESC LIMIT 1
                """
            ),
            {"project_id": project_id, "suite_id": suite_id},
        )
    ).first()
    return row.id if row else None


def _json(value: Any) -> str:
    import json

    return json.dumps(value)
