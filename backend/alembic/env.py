"""Alembic environment.

Runs synchronously against `settings.sync_database_url` (the same URL with the asyncpg driver
stripped). Migrations are short DDL statements; there is nothing to gain from doing them
asynchronously, and a sync engine keeps `alembic` usable from a plain shell and from CI.

The URL is derived from settings *unless a caller supplied one* — via `-x db_url=...` or by
setting the main option programmatically. The integration suite relies on that: it migrates a
throwaway `crucible_test` database, and an env.py that always overwrote the URL would silently
run those migrations against the developer's real database instead.
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from crucible.config import settings
from crucible.db.base import Base
from crucible.db import models  # noqa: F401  - imported for its side effect of registering tables

config = context.config

_x_args = context.get_x_argument(as_dictionary=True)
DATABASE_URL = _x_args.get("db_url") or config.get_main_option("sqlalchemy.url") or settings.sync_database_url
# `%` is the ini interpolation escape; a password containing one would otherwise blow up here.
config.set_main_option("sqlalchemy.url", DATABASE_URL.replace("%", "%%"))

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def include_object(obj, name, type_, reflected, compare_to):  # type: ignore[no-untyped-def]
    # pgvector creates its own types; autogenerate should not try to manage them.
    return not (type_ == "table" and name in {"vector"})


def run_migrations_offline() -> None:
    context.configure(
        url=DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        include_object=include_object,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            include_object=include_object,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
