# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# Builder: resolve dependencies into a venv and collect static assets.
# ---------------------------------------------------------------------------
FROM python:3.14-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv

# uv is copied from its official image (pinned by tag).
COPY --from=ghcr.io/astral-sh/uv:0.11.7 /uv /bin/uv

WORKDIR /code

# Install dependencies first (cached until the lockfile changes).
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

# Copy the app and collect static files (production settings, DEBUG off).
COPY . .
RUN /opt/venv/bin/python manage.py collectstatic --noinput

# ---------------------------------------------------------------------------
# Runtime: slim image with only the venv, the app, and runtime libs.
# ---------------------------------------------------------------------------
FROM python:3.14-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    # Cap native thread pools (OpenBLAS in the cv2 wheel, OpenMP). ONNX Runtime
    # has its own cap, ONNX_INTRA_OP_THREADS (default 2).
    OMP_NUM_THREADS=2 \
    OPENBLAS_NUM_THREADS=2

# opencv-python-headless needs libglib2.0 at runtime (no OpenGL required).
# Create an unprivileged user to run the app.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --create-home --uid 10001 appuser

WORKDIR /code

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder --chown=appuser:appuser /code /code

USER appuser

EXPOSE 8050

# Liveness probe: check gunicorn is accepting connections. (A TCP check avoids
# Django's ALLOWED_HOSTS validation; use GET /healthz for readiness probes in
# orchestration, adding the probe host to ALLOWED_HOSTS.)
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import socket; socket.create_connection(('127.0.0.1', 8050), 2).close()"]

# ALLOWED_HOSTS and ELN_ORIGIN are provided at runtime (see README); SECRET_KEY
# is optional (see config/settings.py).
# Worker/threads/timeout are configurable via GUNICORN_* env vars.
CMD ["gunicorn", "config.wsgi:application", "-c", "gunicorn.conf.py"]
