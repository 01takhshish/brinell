"""Runs: create, list, inspect, cancel.

Creating a run does not execute anything — it enqueues `run_cases` rows. Workers pick them up (see
`runner/worker.py`), which is what lets the API return in milliseconds while a 500-case suite runs
for ten minutes, and what lets a worker crash without taking the request with it.
"""

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Query
from sqlalchemy import text

from crucible.api.deps import CurrentPrincipal, Db, ProjectId, require_role
from crucible.api.schemas import Page, RunCaseDetail, RunCaseOut, RunCreate, RunCreated, RunOut
from crucible.errors import NotFound
from crucible.runner.service import CreateRunRequest, cancel_run, create_run

router = APIRouter(prefix="/projects/{project_id}/runs", tags=["runs"])

# Each list endpoint runs its filter twice: once to count the matching rows and once to fetch the
# page. The two have to agree. A total computed over a different WHERE than the rows is worse than
# no total at all, because it is wrong in a way that looks authoritative — so the predicate lives in
# one string that both queries interpolate, and it cannot drift.
RUN_FILTER = """
    r.project_id = :p
    AND (CAST(:status AS text) IS NULL OR r.status = CAST(:status AS text))
    AND (CAST(:suite AS uuid) IS NULL OR r.suite_id = CAST(:suite AS uuid))
"""

CASE_FILTER = """
    run_id = :r AND (CAST(:status AS text) IS NULL OR status = CAST(:status AS text))
"""

RUN_COLUMNS = """
    r.id, r.status, r.label, r.trigger, r.git_sha, r.git_branch,
    r.target_version_id, r.suite_id, r.dataset_version_id, r.baseline_run_id,
    r.total_cases, r.completed_cases, r.passed_cases, r.errored_cases,
    r.pass_rate, r.mean_score, r.total_cost_micros, r.total_input_tokens, r.total_output_tokens,
    r.cache_hits, r.p50_latency_ms, r.p95_latency_ms, r.p99_latency_ms,
    r.error, r.started_at, r.finished_at, r.created_at
"""


@router.post("", response_model=RunCreated, status_code=202)
async def start_run(body: RunCreate, project_id: ProjectId, db: Db, principal: CurrentPrincipal) -> RunCreated:
    """202, not 201: the run is accepted and queued, and its result does not exist yet."""
    await require_role(project_id, db, principal, "member")
    created = await create_run(
        db,
        CreateRunRequest(
            project_id=project_id,
            target_version_id=body.target_version_id,
            suite_id=body.suite_id,
            dataset_version_id=body.dataset_version_id,
            baseline_run_id=body.baseline_run_id,
            concurrency=body.concurrency,
            max_attempts=body.max_attempts,
            trigger=body.trigger,
            git_sha=body.git_sha,
            git_branch=body.git_branch,
            label=body.label,
            idempotency_key=body.idempotency_key,
        ),
    )
    return RunCreated(
        id=created.run_id,
        total_cases=created.total_cases,
        reused=created.reused,
        baseline_run_id=created.baseline_run_id,
    )


@router.get("", response_model=Page[RunOut])
async def list_runs(
    project_id: ProjectId,
    db: Db,
    status: Annotated[str | None, Query()] = None,
    suite_id: Annotated[UUID | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Any:
    """A page of runs, newest first, with the total so a caller knows what it is not seeing.

    This used to return a bare list. The limit was still enforced, so a project with more runs than
    the page size got the newest N and no indication that anything had been left behind — the
    dashboard looked complete and was not. Returning the total is what makes the truncation visible;
    it is the difference between a page and a silently clipped list.
    """
    filters = {"p": project_id, "status": status, "suite": suite_id}
    total = (await db.execute(text(f"SELECT count(*) FROM runs r WHERE {RUN_FILTER}"), filters)).scalar_one()
    items = (
        await db.execute(
            text(
                f"""
                SELECT {RUN_COLUMNS}
                FROM runs r
                WHERE {RUN_FILTER}
                ORDER BY r.created_at DESC
                LIMIT :limit OFFSET :offset
                """  # both interpolations are module constants; every value is a bound parameter
            ),
            {**filters, "limit": limit, "offset": offset},
        )
    ).all()
    return {"items": items, "total": total, "limit": limit, "offset": offset}


async def _load_run(db: Db, project_id: UUID, run_id: UUID) -> Any:
    row = (
        await db.execute(
            text(f"SELECT {RUN_COLUMNS} FROM runs r WHERE r.id = :id AND r.project_id = :p"),
            {"id": run_id, "p": project_id},
        )
    ).first()
    if row is None:
        raise NotFound("Run")
    return row


@router.get("/{run_id}", response_model=RunOut)
async def get_run(run_id: UUID, project_id: ProjectId, db: Db) -> Any:
    return await _load_run(db, project_id, run_id)


@router.get("/{run_id}/cases", response_model=Page[RunCaseOut])
async def list_run_cases(
    run_id: UUID,
    project_id: ProjectId,
    db: Db,
    status: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Any:
    """A page of cases within one run. See `list_runs` for why the total is part of the response.

    The ceiling matters more here than on runs: a 500-case suite is ordinary, and the case table is
    what the run detail page renders. Without a total, a suite larger than the page size showed a
    pass rate computed over a subset of its own cases.
    """
    await _load_run(db, project_id, run_id)
    filters = {"r": run_id, "status": status}
    total = (await db.execute(text(f"SELECT count(*) FROM run_cases WHERE {CASE_FILTER}"), filters)).scalar_one()
    items = (
        await db.execute(
            text(
                f"""
                SELECT id, external_id, status, score, attempt, latency_ms, cost_micros,
                       cache_hit, error, finished_at
                FROM run_cases
                WHERE {CASE_FILTER}
                ORDER BY external_id
                LIMIT :limit OFFSET :offset
                """  # CASE_FILTER is a module constant; every value is a bound parameter
            ),
            {**filters, "limit": limit, "offset": offset},
        )
    ).all()
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/{run_id}/cases/{run_case_id}", response_model=RunCaseDetail)
async def get_run_case(run_case_id: UUID, run_id: UUID, project_id: ProjectId, db: Db) -> Any:
    """The trace view: what went in, what came back, which assertions fired, and every span.

    Spans are filtered to the case's final attempt. Earlier attempts are still on disk, but showing
    all of them by default makes a retried case look like it called the model twice as often.
    """
    await _load_run(db, project_id, run_id)
    case = (
        await db.execute(
            text(
                """
                SELECT rc.id, rc.external_id, rc.status, rc.score, rc.attempt, rc.latency_ms,
                       rc.cost_micros, rc.cache_hit, rc.error, rc.finished_at, rc.output,
                       rc.assertion_results, c.input, c.expected
                FROM run_cases rc JOIN cases c ON c.id = rc.case_id
                WHERE rc.id = :id AND rc.run_id = :r
                """
            ),
            {"id": run_case_id, "r": run_id},
        )
    ).first()
    if case is None:
        raise NotFound("Run case")

    spans = (
        await db.execute(
            text(
                """
                SELECT id, parent_id, attempt, seq, kind, name, provider, model,
                       request, response, input_tokens, output_tokens, cost_micros,
                       latency_ms, cache_hit, retries, error, started_at
                FROM spans
                WHERE run_case_id = :id AND attempt = (SELECT max(attempt) FROM spans WHERE run_case_id = :id)
                ORDER BY seq
                """
            ),
            {"id": run_case_id},
        )
    ).all()

    return {
        **case._mapping,
        "assertion_results": case.assertion_results or [],
        "spans": [dict(s._mapping) for s in spans],
    }


@router.post("/{run_id}/cancel", response_model=RunOut)
async def cancel(run_id: UUID, project_id: ProjectId, db: Db, principal: CurrentPrincipal) -> Any:
    await require_role(project_id, db, principal, "member")
    await _load_run(db, project_id, run_id)
    # Cancelling a finished run is a no-op rather than an error: CI cancels on a cancelled job, and
    # racing with the final case should not turn a green build red.
    await cancel_run(db, project_id, run_id)
    return await _load_run(db, project_id, run_id)
