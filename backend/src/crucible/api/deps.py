"""Request-scoped dependencies: who is calling, and what may they touch.

Authorization is deliberately boring and centralised in `project_scope`. Every project-scoped route
resolves the caller's access through it, so "can this caller see this project" is answered in one
place rather than re-derived per handler — the kind of check that goes wrong quietly when it is
spread across thirty endpoints.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from crucible.api.security import decode_access_token, hash_api_key
from crucible.db.session import get_db
from crucible.errors import Forbidden, NotFound, Unauthorized

Db = Annotated[AsyncSession, Depends(get_db)]

ROLE_RANK = {"member": 0, "admin": 1, "owner": 2}


@dataclass(frozen=True)
class Principal:
    """Either a signed-in user or a CI key. `project_id` is set only for keys, which are scoped to
    exactly one project and can never reach another."""

    kind: str  # "user" | "api_key"
    user_id: UUID | None = None
    api_key_id: UUID | None = None
    project_id: UUID | None = None
    is_admin: bool = False

    @property
    def label(self) -> str:
        return f"{self.kind}:{self.user_id or self.api_key_id}"


def _bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    return token.strip() if scheme.lower() == "bearer" and token.strip() else None


async def current_principal(
    request: Request,
    db: Db,
    authorization: Annotated[str | None, Header()] = None,
    x_api_key: Annotated[str | None, Header()] = None,
) -> Principal:
    key = x_api_key or _api_key_from_bearer(authorization)
    if key:
        return await _principal_from_api_key(db, key)

    token = _bearer(authorization) or request.cookies.get("crucible_session")
    if not token:
        raise Unauthorized("no credentials supplied")

    payload = decode_access_token(token)
    row = (
        await db.execute(text("SELECT id, is_admin FROM users WHERE id = :id"), {"id": UUID(payload["sub"])})
    ).first()
    if row is None:
        # The token is valid but the account is gone — a deleted user must not keep an hour of access.
        raise Unauthorized("account no longer exists")
    return Principal(kind="user", user_id=row.id, is_admin=row.is_admin)


def _api_key_from_bearer(authorization: str | None) -> str | None:
    """CI tools reach for `Authorization: Bearer` by habit, so accept a key there too."""
    token = _bearer(authorization)
    return token if token and token.startswith("crk_") else None


async def _principal_from_api_key(db: AsyncSession, plaintext: str) -> Principal:
    row = (
        await db.execute(
            text(
                """
                UPDATE api_keys SET last_used_at = now()
                WHERE key_hash = :h AND revoked_at IS NULL
                RETURNING id, project_id
                """
            ),
            {"h": hash_api_key(plaintext)},
        )
    ).first()
    if row is None:
        raise Unauthorized("invalid or revoked API key")
    return Principal(kind="api_key", api_key_id=row.id, project_id=row.project_id)


CurrentPrincipal = Annotated[Principal, Depends(current_principal)]


async def project_scope(project_id: UUID, db: Db, principal: CurrentPrincipal) -> UUID:
    """The single gate for project-scoped routes. Returns the project id once access is proven."""
    if principal.kind == "api_key":
        if principal.project_id != project_id:
            # Not a 403: a key must not be able to probe which project ids exist.
            raise NotFound("Project")
        return project_id

    exists = (await db.execute(text("SELECT 1 FROM projects WHERE id = :id"), {"id": project_id})).first()
    if exists is None:
        raise NotFound("Project")
    if principal.is_admin:
        return project_id

    member = (
        await db.execute(
            text("SELECT role FROM project_members WHERE project_id = :p AND user_id = :u"),
            {"p": project_id, "u": principal.user_id},
        )
    ).first()
    if member is None:
        raise NotFound("Project")
    return project_id


ProjectId = Annotated[UUID, Depends(project_scope)]


async def require_role(project_id: UUID, db: AsyncSession, principal: Principal, minimum: str) -> None:
    """Writes that change who can do what need more than membership."""
    if principal.is_admin or principal.kind == "api_key":
        return
    row = (
        await db.execute(
            text("SELECT role FROM project_members WHERE project_id = :p AND user_id = :u"),
            {"p": project_id, "u": principal.user_id},
        )
    ).first()
    if row is None or ROLE_RANK.get(row.role, -1) < ROLE_RANK[minimum]:
        raise Forbidden(f"requires {minimum} on this project")
