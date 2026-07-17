# 2026-07-17 (DEPLOY): production image for fly.io — uv-locked, non-root.
# Build context is the repo root; .dockerignore keeps secrets/data/tests out.
# Runbook: docs/deploy.md.

FROM python:3.14-slim

# uv binary pinned to the version that generated uv.lock (reproducible).
COPY --from=ghcr.io/astral-sh/uv:0.9.26 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    # System python is 3.14 (base image); never download a managed one.
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependency layer first so source edits don't bust the deps cache.
# README.md is required by hatchling (referenced from pyproject.toml).
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# Source layer; second sync installs the bacchus-mm project itself.
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# Non-root. /app/data is the fly volume mount point — logging.dir stays the
# relative "data" default, so the DB/JSONL land on the volume with no config
# change. (If a pre-existing volume is root-owned, fix once from an ssh
# console: chown -R bacchus:bacchus /app/data — see docs/deploy.md.)
RUN groupadd --gid 10001 bacchus \
    && useradd --uid 10001 --gid bacchus --no-create-home --shell /usr/sbin/nologin bacchus \
    && mkdir -p /app/data \
    && chown -R bacchus:bacchus /app/data
USER bacchus

ENV PATH="/app/.venv/bin:$PATH" \
    # Force-enables the /health endpoint (see config.py); fly.toml [checks]
    # polls it on this port.
    HEALTH_PORT=8080

# Default: trade per config (demo unless KALSHI_ENV/credentials say prod).
# fly.toml [processes] overrides this command (e.g. adds --live for prod).
CMD ["bacchus-mm", "run"]
