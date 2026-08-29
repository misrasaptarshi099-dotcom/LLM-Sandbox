# syntax=docker/dockerfile:1
# ------------------------------------------------------------------------------
# Multi-stage minimal Dockerfile for LLM Sandbox
# Runs as dedicated non-root user (UID 10001) with dropped capabilities
# ------------------------------------------------------------------------------

# --- Stage 1: Build stage ---
FROM python:3.12-slim-bookworm AS builder

# Install uv for fast, reliable package installation
COPY --from=ghcr.io/astral-sh/uv:0.6.5 /uv /bin/uv

WORKDIR /build

# Copy dependency specifications first for layer caching
COPY pyproject.toml uv.lock ./

# Install application dependencies into virtual environment
RUN uv sync --frozen --no-dev --no-install-project

# Copy source and sync project
COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./
RUN uv sync --frozen --no-dev

# --- Stage 2: Minimal runtime stage ---
FROM python:3.12-slim-bookworm AS runtime

# Security: create dedicated non-root system user and group (UID/GID 10001)
RUN groupadd -g 10001 appgroup && \
    useradd -u 10001 -g appgroup -s /sbin/nologin -M appuser

WORKDIR /app

# Copy virtualenv from builder
COPY --from=builder --chown=appuser:appgroup /build/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Copy application files with ownership
COPY --chown=appuser:appgroup app ./app
COPY --chown=appuser:appgroup migrations ./migrations
COPY --chown=appuser:appgroup alembic.ini ./

# Switch to non-root user
USER appuser

# Healthcheck against liveness endpoint
HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health/live')" || exit 1

EXPOSE 8000

# Default command runs the API server
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
