# syntax=docker/dockerfile:1

# --- 1. Widget: builds widget/dist/widget.js, served by the API at /widget.v1.js ---------------
FROM node:24-slim AS widget
WORKDIR /widget
COPY widget/package.json widget/package-lock.json ./
RUN npm ci
COPY widget/ ./
RUN npm run build

# --- 2. Python dependencies + project ------------------------------------------------------------
FROM python:3.12-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.13.9 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY api ./api
COPY core ./core
COPY agents ./agents
COPY cli ./cli
COPY evals ./evals
COPY alembic ./alembic
COPY alembic.ini ./
COPY tenants ./tenants
COPY faq ./faq
RUN uv sync --frozen --no-dev

# --- 3. Runtime ------------------------------------------------------------------------------------
FROM python:3.12-slim
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --home-dir /app --shell /usr/sbin/nologin app
WORKDIR /app
COPY --from=builder --chown=app:app /app /app
COPY --from=widget --chown=app:app /widget/dist /app/widget/dist
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER app
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"]
# One process: the rate limiter is in-memory (ADR-006). uvicorn must NOT interpret
# X-Forwarded-For; the app does (TRUSTED_PROXY_COUNT=1 behind Caddy). See ADR-008.
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers"]
