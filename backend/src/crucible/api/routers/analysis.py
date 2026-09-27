"""Comparison and failure clustering — the two questions a run leaves you with.

"Did this change break anything?" is `compare`, and it is what CI gates on. "What broke, and is it
one bug or twenty?" is `clusters`, which groups failures so a 60-failure run reads as three
problems instead of sixty rows.
"""

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Query
from sqlalchemy import text

from crucible.analysis.cluster import cluster_run, similar_cases
from crucible.analysis.compare import compare_runs
from crucible.api.deps import Db, ProjectId
from crucible.errors import BadRequest, NotFound

router = APIRouter(prefix="/projects/{project_id}", tags=["analysis"])


async def _assert_run(db: Db, project_id: UUID, run_id: UUID) -> None:
    row = (
        await db.execute(text("SELECT 1 FROM runs WHERE id = :id AND project_id = :p"), {"id": run_id, "p": project_id})
    ).first()
    if row is None:
        raise NotFound("Run")


@router.get("/runs/{run_id}/compare")
async def compare(
    run_id: UUID,
    project_id: ProjectId,
    db: Db,
    baseline: Annotated[UUID | None, Query(description="Baseline run id; defaults to the run's own baseline")] = None,
) -> dict[str, Any]:
    await _assert_run(db, project_id, run_id)
    baseline_id = baseline
    if baseline_id is None:
        row = (await db.execute(text("SELECT baseline_run_id FROM runs WHERE id = :id"), {"id": run_id})).one()
        baseline_id = row.baseline_run_id
    if baseline_id is None:
        raise BadRequest("this run has no baseline; pass ?baseline=<run_id>")

    comparison = await compare_runs(db, project_id=project_id, baseline_run_id=baseline_id, current_run_id=run_id)
    return comparison.as_dict()


@router.get("/runs/{run_id}/clusters")
async def get_clusters(run_id: UUID, project_id: ProjectId, db: Db) -> dict[str, Any]:
    """Reads what was computed; it does not recompute.

    Clustering embeds every failure, so making a page load do that work would put a variable-cost
    operation behind a GET. `POST /clusters` is the explicit recompute.
    """
    await _assert_run(db, project_id, run_id)
    rows = (
        await db.execute(
            text(
                """
                SELECT fc.id, fc.label, fc.summary, fc.size, fc.top_terms,
                       cm.run_case_id, cm.distance, rc.external_id, rc.status
                FROM failure_clusters fc
                LEFT JOIN cluster_members cm ON cm.cluster_id = fc.id
                LEFT JOIN run_cases rc ON rc.id = cm.run_case_id
                WHERE fc.run_id = :r
                ORDER BY fc.size DESC, fc.id, cm.distance
                """
            ),
            {"r": run_id},
        )
    ).all()

    clusters: dict[UUID, dict[str, Any]] = {}
    for row in rows:
        cluster = clusters.setdefault(
            row.id,
            {
                "id": str(row.id),
                "label": row.label,
                "summary": row.summary,
                "size": row.size,
                "top_terms": list(row.top_terms or []),
                "members": [],
            },
        )
        if row.run_case_id is not None:
            cluster["members"].append(
                {
                    "run_case_id": str(row.run_case_id),
                    "external_id": row.external_id,
                    "status": row.status,
                    "distance": round(row.distance, 4),
                }
            )
    return {"run_id": str(run_id), "clusters": list(clusters.values())}


@router.post("/runs/{run_id}/clusters", status_code=200)
async def recompute_clusters(
    run_id: UUID,
    project_id: ProjectId,
    db: Db,
    threshold: Annotated[float | None, Query(ge=0.05, le=0.95)] = None,
) -> dict[str, Any]:
    await _assert_run(db, project_id, run_id)
    kwargs: dict[str, Any] = {}
    if threshold is not None:
        kwargs["threshold"] = threshold
    clusters = await cluster_run(db, project_id=project_id, run_id=run_id, **kwargs)
    return {"run_id": str(run_id), "clusters": [c.as_dict() for c in clusters]}


@router.get("/run-cases/{run_case_id}/similar")
async def similar(
    run_case_id: UUID,
    project_id: ProjectId,
    db: Db,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> dict[str, Any]:
    """ "Has this failure happened before?" — nearest neighbours over stored failure embeddings."""
    rows = await similar_cases(db, project_id=project_id, run_case_id=run_case_id, limit=limit)
    return {"run_case_id": str(run_case_id), "similar": rows}
