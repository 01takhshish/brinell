"""Signup, login, and the current user.

The session token is returned in the body *and* set as an HttpOnly cookie. The body is for the CLI
and for CI; the cookie is for the browser app, where storing a JWT in localStorage would mean any
XSS is a full account takeover.
"""

from typing import Any

from fastapi import APIRouter, Request, Response
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from crucible.api.deps import CurrentPrincipal, Db
from crucible.api.schemas import LoginRequest, SignupRequest, TokenResponse, UserOut
from crucible.api.security import create_access_token, hash_password, needs_rehash, verify_password
from crucible.api.throttle import check as throttle
from crucible.config import settings
from crucible.errors import Conflict, Unauthorized
from crucible.logging import get_logger

router = APIRouter(prefix="/auth", tags=["auth"])
log = get_logger(__name__)

COOKIE_NAME = "crucible_session"


def _set_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=settings.access_token_ttl_seconds,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )


def _client_ip(request: Request) -> str:
    """The peer address as uvicorn reports it.

    Behind a proxy this is only the real client when uvicorn runs with `--proxy-headers` and a
    `--forwarded-allow-ips` naming that proxy (see RUNBOOK). When it is not — everyone shares the
    proxy's address — the per-IP throttle degrades into a global cap on attempts, which is the
    acceptable failure direction: too much throttling under attack, never none.
    """
    return request.client.host if request.client else "unknown"


@router.post("/signup", response_model=TokenResponse, status_code=201)
async def signup(body: SignupRequest, db: Db, request: Request, response: Response) -> TokenResponse:
    # Mass account creation is the abuse here, so the budget is per-address and generous for a
    # human: nobody legitimately signs up twenty times in an hour from one machine.
    await throttle("signup:ip", _client_ip(request), limit=20, window_seconds=3600)
    try:
        user_id = (
            await db.execute(
                text(
                    """
                    INSERT INTO users (id, email, name, password_hash)
                    VALUES (gen_random_uuid(), lower(:e), :n, :h)
                    RETURNING id
                    """
                ),
                {"e": body.email, "n": body.name, "h": hash_password(body.password)},
            )
        ).scalar_one()
    except IntegrityError as exc:
        raise Conflict("an account with that email already exists") from exc

    token = create_access_token(user_id)
    _set_cookie(response, token)
    log.info("user_signed_up", user_id=str(user_id))
    return TokenResponse(access_token=token, expires_in=settings.access_token_ttl_seconds)


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, db: Db, request: Request, response: Response) -> TokenResponse:
    # Two dimensions because they stop different attacks: per-email stops a distributed guess
    # against one account (many IPs, one victim), per-IP stops one machine spraying the user table
    # (one attacker, many victims). Both are counted before the Argon2 verify — the throttle is
    # cheap and the hash is not, so the order is the protection.
    await throttle("login:email", body.email, limit=10, window_seconds=300)
    await throttle("login:ip", _client_ip(request), limit=30, window_seconds=300)
    row = (
        await db.execute(
            text("SELECT id, password_hash FROM users WHERE email = lower(:e)"),
            {"e": body.email},
        )
    ).first()

    # Same message for "no such user" and "wrong password": distinguishing them turns the login
    # form into an account-enumeration oracle.
    if row is None or not verify_password(body.password, row.password_hash):
        raise Unauthorized("invalid email or password")

    # Argon2 parameters get raised over time; a successful login is the only moment the plaintext
    # is available to re-hash with the current cost.
    if needs_rehash(row.password_hash):
        await db.execute(
            text("UPDATE users SET password_hash = :h WHERE id = :id"),
            {"h": hash_password(body.password), "id": row.id},
        )

    token = create_access_token(row.id)
    _set_cookie(response, token)
    return TokenResponse(access_token=token, expires_in=settings.access_token_ttl_seconds)


@router.post("/logout", status_code=204)
async def logout(response: Response) -> Response:
    response.delete_cookie(COOKIE_NAME, path="/")
    response.status_code = 204
    return response


@router.get("/me", response_model=UserOut)
async def me(principal: CurrentPrincipal, db: Db) -> Any:
    if principal.kind != "user":
        raise Unauthorized("this endpoint requires a user session, not an API key")
    row = (
        await db.execute(text("SELECT id, email, name, is_admin FROM users WHERE id = :id"), {"id": principal.user_id})
    ).one()
    return row
