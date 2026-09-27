"""Projects and their API keys.

A project is the tenancy boundary: every dataset, suite, run and key belongs to exactly one, and
`project_scope` in `deps.py` is the only place that decides whether a caller may cross it.
"""

from typing import Any
from uuid import UUID

from fastapi import APIRouter
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from crucible.api.deps import CurrentPrincipal, Db, ProjectId, require_role
from crucible.api.schemas import ApiKeyCreate, ApiKeyCreated, ApiKeyOut, ProjectCreate, ProjectOut
from crucible.api.security import generate_api_key
from crucible.errors import Conflict, Forbidden, NotFound
from crucible.logging import get_logger

router = APIRouter(prefix="/projects", tags=["projects"])
log = get_logger(__name__)


def _require_user(principal: CurrentPrincipal) -> UUID:
    """API keys administer nothing: a leaked CI key must not be able to mint more keys."""
    if principal.kind != "user" or principal.user_id is None:
        raise Forbidden("this endpoint requires a user session")
    return principal.user_id


@router.get("", response_model=list[ProjectOut])
async def list_projects(db: Db, principal: CurrentPrincipal) -> Any:
    if principal.kind == "api_key":
        return (
            await db.execute(
                text("SELECT p.*, NULL AS role FROM projects p WHERE p.id = :id"), {"id": principal.project_id}
            )
        ).all()
    if principal.is_admin:
        return (await db.execute(text("SELECT p.*, 'owner' AS role FROM projects p ORDER BY p.created_at"))).all()
    return (
        await db.execute(
            text(
                """
                SELECT p.*, m.role
                FROM projects p JOIN project_members m ON m.project_id = p.id
                WHERE m.user_id = :u
                ORDER BY p.created_at
                """
            ),
            {"u": principal.user_id},
        )
    ).all()


@router.post("", response_model=ProjectOut, status_code=201)
async def create_project(body: ProjectCreate, db: Db, principal: CurrentPrincipal) -> Any:
    user_id = _require_user(principal)
    try:
        project = (
            await db.execute(
                text("INSERT INTO projects (id, name, slug) VALUES (gen_random_uuid(), :n, :s) RETURNING *"),
                {"n": body.name, "s": body.slug},
            )
        ).one()
    except IntegrityError as exc:
        raise Conflict(f"slug {body.slug!r} is already taken") from exc

    # The creator is the owner. Without this the project would exist with nobody able to reach it.
    await db.execute(
        text(
            """
            INSERT INTO project_members (id, project_id, user_id, role)
            VALUES (gen_random_uuid(), :p, :u, 'owner')
            """
        ),
        {"p": project.id, "u": user_id},
    )
    log.info("project_created", project_id=str(project.id), slug=body.slug)
    return {**project._mapping, "role": "owner"}


@router.get("/{project_id}", response_model=ProjectOut)
async def get_project(project_id: ProjectId, db: Db) -> Any:
    row = (await db.execute(text("SELECT * FROM projects WHERE id = :id"), {"id": project_id})).first()
    if row is None:
        raise NotFound("Project")
    return row


@router.get("/{project_id}/keys", response_model=list[ApiKeyOut])
async def list_keys(project_id: ProjectId, db: Db, principal: CurrentPrincipal) -> Any:
    _require_user(principal)
    await require_role(project_id, db, principal, "admin")
    return (
        await db.execute(
            text(
                """
                SELECT id, name, prefix, created_at, last_used_at, revoked_at
                FROM api_keys WHERE project_id = :p ORDER BY created_at DESC
                """
            ),
            {"p": project_id},
        )
    ).all()


@router.post("/{project_id}/keys", response_model=ApiKeyCreated, status_code=201)
async def create_key(body: ApiKeyCreate, project_id: ProjectId, db: Db, principal: CurrentPrincipal) -> Any:
    user_id = _require_user(principal)
    await require_role(project_id, db, principal, "admin")

    plaintext, prefix, key_hash = generate_api_key()
    row = (
        await db.execute(
            text(
                """
                INSERT INTO api_keys (id, project_id, name, prefix, key_hash, created_by)
                VALUES (gen_random_uuid(), :p, :n, :pre, :h, :u)
                RETURNING id, name, prefix, created_at, last_used_at, revoked_at
                """
            ),
            {"p": project_id, "n": body.name, "pre": prefix, "h": key_hash, "u": user_id},
        )
    ).one()
    log.info("api_key_created", project_id=str(project_id), key_id=str(row.id), prefix=prefix)
    # Only response that ever contains the secret; the database holds a hash and cannot recover it.
    return {**row._mapping, "key": plaintext}


@router.delete("/{project_id}/keys/{key_id}", status_code=204)
async def revoke_key(key_id: UUID, project_id: ProjectId, db: Db, principal: CurrentPrincipal) -> None:
    _require_user(principal)
    await require_role(project_id, db, principal, "admin")
    row = (
        await db.execute(
            text(
                """
                UPDATE api_keys SET revoked_at = now()
                WHERE id = :id AND project_id = :p AND revoked_at IS NULL
                RETURNING id
                """
            ),
            {"id": key_id, "p": project_id},
        )
    ).first()
    if row is None:
        raise NotFound("API key")
    log.info("api_key_revoked", project_id=str(project_id), key_id=str(key_id))
