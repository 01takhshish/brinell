"""Targets, datasets and suites — the things a run points at.

All three are project-scoped and all three are reached through `ProjectId`, so authorization is
decided once in `deps.project_scope` rather than re-implemented per handler.
"""

from typing import Any
from uuid import UUID

from fastapi import APIRouter
from sqlalchemy import text

from crucible.api import registry
from crucible.api.deps import CurrentPrincipal, Db, ProjectId, require_role
from crucible.api.schemas import (
    DatasetCreate,
    DatasetOut,
    DatasetVersionCreate,
    DatasetVersionOut,
    SuiteCreate,
    SuiteOut,
    TargetCreate,
    TargetOut,
    TargetVersionCreate,
    TargetVersionOut,
)
from crucible.domain.assertions import parse_assertions
from crucible.errors import BadRequest, NotFound

router = APIRouter(prefix="/projects/{project_id}", tags=["registry"])


# ─── Targets ─────────────────────────────────────────────────────────────────


@router.get("/targets", response_model=list[TargetOut])
async def list_targets(project_id: ProjectId, db: Db) -> Any:
    return (
        await db.execute(
            text(
                """
                SELECT t.*, (
                    SELECT tv.semver FROM target_versions tv
                    WHERE tv.target_id = t.id ORDER BY tv.created_at DESC LIMIT 1
                ) AS latest_version
                FROM targets t WHERE t.project_id = :p ORDER BY t.created_at DESC
                """
            ),
            {"p": project_id},
        )
    ).all()


@router.post("/targets", response_model=TargetOut, status_code=201)
async def create_target(body: TargetCreate, project_id: ProjectId, db: Db, principal: CurrentPrincipal) -> Any:
    await require_role(project_id, db, principal, "member")
    row = await registry.create_target(
        db, project_id=project_id, name=body.name, kind=body.kind, description=body.description
    )
    return {**row._mapping, "latest_version": None}


@router.get("/targets/{target_id}/versions", response_model=list[TargetVersionOut])
async def list_target_versions(target_id: UUID, project_id: ProjectId, db: Db) -> Any:
    await registry.get_target(db, project_id=project_id, target_id=target_id)
    return (
        await db.execute(
            text("SELECT * FROM target_versions WHERE target_id = :t ORDER BY created_at DESC"),
            {"t": target_id},
        )
    ).all()


@router.post("/targets/{target_id}/versions", response_model=TargetVersionOut, status_code=201)
async def push_target_version(
    body: TargetVersionCreate, target_id: UUID, project_id: ProjectId, db: Db, principal: CurrentPrincipal
) -> Any:
    """Pushing an unchanged config returns the existing version with `reused: true` (HTTP 201 either
    way — the caller's desired state is satisfied, and branching the status code buys nothing)."""
    await require_role(project_id, db, principal, "member")
    if "model" not in body.config:
        raise BadRequest("target config must name a `model`")
    pushed = await registry.push_target_version(
        db,
        project_id=project_id,
        target_id=target_id,
        config=body.config,
        semver=body.semver,
        notes=body.notes,
        created_by=principal.user_id,
    )
    return {**pushed.row._mapping, "reused": pushed.reused}


# ─── Datasets ────────────────────────────────────────────────────────────────


@router.get("/datasets", response_model=list[DatasetOut])
async def list_datasets(project_id: ProjectId, db: Db) -> Any:
    return (
        await db.execute(
            text(
                """
                SELECT d.*, dv.version AS latest_version, coalesce(dv.case_count, 0) AS case_count
                FROM datasets d
                LEFT JOIN LATERAL (
                    SELECT version, case_count FROM dataset_versions
                    WHERE dataset_id = d.id ORDER BY version DESC LIMIT 1
                ) dv ON true
                WHERE d.project_id = :p
                ORDER BY d.created_at DESC
                """
            ),
            {"p": project_id},
        )
    ).all()


@router.post("/datasets", response_model=DatasetOut, status_code=201)
async def create_dataset(body: DatasetCreate, project_id: ProjectId, db: Db, principal: CurrentPrincipal) -> Any:
    await require_role(project_id, db, principal, "member")
    row = await registry.create_dataset(db, project_id=project_id, name=body.name, description=body.description)
    return {**row._mapping, "latest_version": None, "case_count": 0}


@router.post("/datasets/{dataset_id}/versions", response_model=DatasetVersionOut, status_code=201)
async def push_dataset_version(
    body: DatasetVersionCreate, dataset_id: UUID, project_id: ProjectId, db: Db, principal: CurrentPrincipal
) -> Any:
    await require_role(project_id, db, principal, "member")
    pushed = await registry.push_dataset_version(
        db,
        project_id=project_id,
        dataset_id=dataset_id,
        cases=[c.model_dump() for c in body.cases],
    )
    return {**pushed.row._mapping, "reused": pushed.reused}


@router.get("/datasets/{dataset_id}/versions", response_model=list[DatasetVersionOut])
async def list_dataset_versions(dataset_id: UUID, project_id: ProjectId, db: Db) -> Any:
    await registry.get_dataset(db, project_id=project_id, dataset_id=dataset_id)
    return (
        await db.execute(
            text("SELECT * FROM dataset_versions WHERE dataset_id = :d ORDER BY version DESC"), {"d": dataset_id}
        )
    ).all()


@router.get("/datasets/{dataset_id}/versions/{version}/cases")
async def list_cases(dataset_id: UUID, version: int, project_id: ProjectId, db: Db) -> dict[str, Any]:
    dv = (
        await db.execute(
            text("SELECT id FROM dataset_versions WHERE dataset_id = :d AND version = :v AND project_id = :p"),
            {"d": dataset_id, "v": version, "p": project_id},
        )
    ).first()
    if dv is None:
        raise NotFound("Dataset version")
    rows = (
        await db.execute(
            text(
                """
                SELECT external_id, input, expected, tags, weight
                FROM cases WHERE dataset_version_id = :dv ORDER BY external_id
                """
            ),
            {"dv": dv.id},
        )
    ).all()
    return {"version": version, "cases": [dict(r._mapping) for r in rows]}


# ─── Suites ──────────────────────────────────────────────────────────────────


@router.get("/suites", response_model=list[SuiteOut])
async def list_suites(project_id: ProjectId, db: Db) -> Any:
    return (
        await db.execute(text("SELECT * FROM suites WHERE project_id = :p ORDER BY created_at DESC"), {"p": project_id})
    ).all()


@router.post("/suites", response_model=SuiteOut, status_code=201)
async def create_suite(body: SuiteCreate, project_id: ProjectId, db: Db, principal: CurrentPrincipal) -> Any:
    await require_role(project_id, db, principal, "member")
    # Validate here rather than at run time: a typo in an assertion should cost zero model calls.
    try:
        parse_assertions(body.assertions)
    except Exception as exc:
        raise BadRequest(f"invalid assertions: {exc}") from exc

    return await registry.create_suite(
        db,
        project_id=project_id,
        name=body.name,
        dataset_id=body.dataset_id,
        assertions=body.assertions,
        judge_model=body.judge_model,
        pass_threshold=body.pass_threshold,
        description=body.description,
    )
