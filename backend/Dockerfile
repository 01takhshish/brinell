# One image for both backend processes. The API and the worker are the same package with the same
# dependencies — separate images would just be two builds that can drift apart. The worker is this
# image with the command overridden:
#
#   docker run <image>                            # API on :8000
#   docker run <image> crucible worker            # eval worker
#   docker run <image> alembic upgrade head       # migrations (run once per deploy, before either)
#
# Migrations are deliberately NOT run on container start: two replicas racing `alembic upgrade` on
# the same database is a coin flip, and a failed migration should stop a deploy, not crash-loop it.

FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app

# Dependencies first, source second: the lockfile changes rarely and the source constantly, so this
# split is the difference between a 5-second rebuild and re-resolving the world on every edit.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY alembic ./alembic
COPY alembic.ini ./
RUN uv sync --frozen --no-dev


FROM python:3.12-slim

# Not root: nothing here needs to bind a low port or write outside /app.
RUN groupadd --system crucible && useradd --system --gid crucible --no-create-home crucible

WORKDIR /app
COPY --from=builder --chown=crucible:crucible /app /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    LOG_JSON=true

USER crucible

# `--proxy-headers` + `--forwarded-allow-ips='*'` make `request.client.host` the real client behind
# the platform's proxy — the auth throttle's per-IP dimension depends on it. The '*' is safe only
# because in every intended deployment the container is not directly reachable; if you expose this
# port to the internet without a proxy, narrow it to the proxy's address (see RUNBOOK.md).
CMD ["uvicorn", "crucible.api.app:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
