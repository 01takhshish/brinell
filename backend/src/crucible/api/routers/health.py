"""Liveness and readiness.

Separate endpoints on purpose: `/healthz` answers "is this process alive" and must not touch the
database, or a database blip will make an orchestrator kill every healthy API pod at once.
`/readyz` answers "should this pod receive traffic" and therefore does check.
"""

from typing import Any

from fastapi import APIRouter, Response
from sqlalchemy import text

from crucible.api.deps import Db
from crucible.config import settings

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {"status": "ok", "env": settings.env}


@router.get("/readyz")
async def readyz(db: Db, response: Response) -> dict[str, Any]:
    checks: dict[str, Any] = {}
    try:
        await db.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:  # readiness reports the failure rather than raising it
        checks["database"] = f"error: {type(exc).__name__}"

    ready = all(v == "ok" for v in checks.values())
    response.status_code = 200 if ready else 503
    return {"status": "ready" if ready else "degraded", "checks": checks}
