# syntax=docker/dockerfile:1
#
# Two-stage build. The first stage compiles dependencies (psycopg and
# cryptography need a toolchain); the second copies only the installed
# packages, so the shipped image carries no compilers. That cuts roughly
# 300 MB and removes build tooling from the runtime attack surface.

# --- Stage 1: build ---------------------------------------------------------
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Dependencies are resolved from pyproject alone first, so this layer is
# cached and only reinstalls when the dependency list actually changes -
# editing application code does not trigger a full reinstall.
COPY pyproject.toml ./
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && mkdir -p app \
    && touch app/__init__.py \
    && /opt/venv/bin/pip install .

# --- Stage 2: runtime -------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH"

# libpq5 only - the client library, not the -dev headers the build needed.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 appuser

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=appuser:appuser app ./app

# Non-root. A container process that does not need root should not have it,
# and this one only needs to open a socket.
USER appuser

EXPOSE 8000

# Hits the liveness endpoint, which deliberately touches no dependency - so a
# database outage cannot make the orchestrator kill a healthy container.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

# One worker by default: the free tier allows 512 MB, and a second worker
# would double the memory and the database connection count for a service
# whose bottleneck is upstream API latency, not CPU. Override WEB_CONCURRENCY
# on a larger instance.
ENV WEB_CONCURRENCY=1
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers ${WEB_CONCURRENCY}"]
