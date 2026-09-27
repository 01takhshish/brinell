"""Integration fixtures: a real Postgres, a real schema, real transactions.

These tests are the only place the queue's guarantees can actually be checked. `SKIP LOCKED`,
lease expiry and the conditional completion update are database behaviours; a mocked session
would assert that the code calls the functions we wrote, which proves nothing about whether two
workers can double-count a case.

The database is created and migrated once per session and truncated between tests. If Postgres
is not reachable the whole directory is skipped rather than failed, so `pytest` on a fresh clone
is green and `make test-all` is the honest signal.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from crucible.config import settings

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL") or settings.database_url.rsplit("/", 1)[0] + "/crucible_test"

TABLES = [
    "cluster_members",
    "failure_clusters",
    "case_embeddings",
    "spans",
    "run_cases",
    "runs",
    "suites",
    "cases",
    "dataset_versions",
    "datasets",
    "target_versions",
    "targets",
    "api_keys",
    "project_members",
    "projects",
    "users",
    "outbox",
]


def _sync_url(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql+psycopg://")


def _ensure_database() -> None:
    """Create `crucible_test` if it does not exist, then migrate it to head."""
    admin_url = _sync_url(TEST_DATABASE_URL).rsplit("/", 1)[0] + "/postgres"
    engine = sa.create_engine(admin_url, isolation_level="AUTOCOMMIT")
    name = TEST_DATABASE_URL.rsplit("/", 1)[1]
    with engine.connect() as conn:
        exists = conn.execute(sa.text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": name}).first()
        if not exists:
            conn.execute(sa.text(f'CREATE DATABASE "{name}"'))
    engine.dispose()

    from alembic import command
    from alembic.config import Config

    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", _sync_url(TEST_DATABASE_URL))
    command.upgrade(cfg, "head")


@pytest.fixture(scope="session", autouse=True)
def database() -> Any:
    try:
        _ensure_database()
    except Exception as exc:  # any connection problem at all means "no database here"
        pytest.skip(f"integration tests need Postgres at {TEST_DATABASE_URL}: {exc}", allow_module_level=True)
    return TEST_DATABASE_URL


@pytest_asyncio.fixture
async def engine(database: str) -> AsyncIterator[Any]:
    """Function-scoped on purpose: asyncpg connections are bound to the event loop that created
    them, and pytest-asyncio gives each test its own loop. A session-scoped engine would work
    until the first test that opens two connections, then fail in a way that looks like a bug in
    the queue rather than in the fixtures."""
    eng = create_async_engine(database, pool_size=20, max_overflow=10, pool_pre_ping=True)
    yield eng
    await eng.dispose()


async def _flush_throttle() -> None:
    """Reset the auth throttle counters, for the same reason the tables are truncated.

    Every test signs up from the same test-client address, so without this the per-IP budget is
    spent by whichever tests happen to run first and the failure lands on an unrelated test later
    in the file — the worst kind of ordering dependency. Redis being down is fine: the throttle
    fails open, so there is nothing to reset.
    """
    from redis.asyncio import Redis

    from crucible.config import settings as app_settings

    r = Redis.from_url(app_settings.redis_url)
    try:
        keys = [k async for k in r.scan_iter("crucible:throttle:*")]
        if keys:
            await r.delete(*keys)
    except Exception:
        pass
    finally:
        await r.aclose()


@pytest_asyncio.fixture
async def sessionmaker(engine: Any) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Truncate before each test rather than wrapping tests in a rollback.

    A rollback-per-test fixture is faster, but it cannot be used here: the queue's whole point is
    that separate workers commit independently, and nesting everything in one outer transaction
    would hide exactly the concurrency this suite is meant to prove.
    """
    maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    async with maker() as session:
        await session.execute(sa.text(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE"))
        await session.commit()
    await _flush_throttle()
    yield maker


@pytest_asyncio.fixture
async def db(sessionmaker: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    async with sessionmaker() as session:
        yield session
        await session.rollback()


@pytest.fixture
def patched_session_scope(sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch) -> None:
    """Point `crucible.db.session.session_scope` at the test database.

    The worker opens its own sessions on purpose — that is the behaviour under test — so the seam
    is the sessionmaker, not an injected session.
    """
    import crucible.db.session as session_module

    monkeypatch.setattr(session_module, "get_sessionmaker", lambda: sessionmaker)
    monkeypatch.setattr("crucible.runner.worker.session_scope", session_module.session_scope)


@pytest_asyncio.fixture
async def asgi_transport(sessionmaker: async_sessionmaker[AsyncSession]) -> AsyncIterator[Any]:
    """The real ASGI app, talking to the test database, without a socket.

    `get_db` is overridden rather than monkeypatched because it is the app's declared seam: the
    override commits on success and rolls back on failure, which is what `session_scope` does in
    production, so handlers see the same transaction semantics they will see live.

    This is a fixture of its own so the CLI tests can build their own client on top of the same
    app — the CLI's job is to be a different caller, not a different backend.
    """
    import httpx

    from crucible.api.app import create_app
    from crucible.db.session import get_db

    async def _override() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app = create_app()
    app.dependency_overrides[get_db] = _override
    transport = httpx.ASGITransport(app=app)
    yield transport
    await transport.aclose()


@pytest_asyncio.fixture
async def client(asgi_transport: Any) -> AsyncIterator[Any]:
    import httpx

    async with httpx.AsyncClient(transport=asgi_transport, base_url="http://api") as http_client:
        yield http_client
