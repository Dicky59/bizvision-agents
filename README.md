# bizvision-agents

Multi-tenant agent platform of BizVision Oy. Phase 1: shared core + AI customer-service assistant.
See `CLAUDE.md` for conventions and `docs/specs/` for the specs.

## Quick start (Git Bash)

```bash
cp .env.example .env            # fill in API keys when needed
docker compose up -d db         # postgres 16 + pgvector (also creates the bv_test database)
uv sync
uv run alembic upgrade head
uv run uvicorn api.main:app --reload --port 8000   # GET /healthz -> 200
```

## Quality gates

```bash
uv run ruff format .
uv run ruff check . --fix
uv run pyright
uv run pytest
```

Tests use the separate `bv_test` database (`TEST_DATABASE_URL`). It is created by
`docker/initdb/01-create-test-db.sql`, which Postgres only runs on a **fresh volume**. If your
volume predates it, either `docker compose down -v && docker compose up -d db` (wipes dev data)
or run `docker compose exec db psql -U bv -d bv -c "CREATE DATABASE bv_test OWNER bv"`.

Live tests (real Anthropic/OpenAI calls, a few cents; write only to `bv_test`):
`uv run pytest -m live` — needs `ANTHROPIC_API_KEY` and `OPENAI_API_KEY` in `.env`.

## Tenants

```bash
uv run bv tenants create bizvision --config tenants/bizvision.yaml   # slug must match the YAML
uv run bv tenants show bizvision
uv run bv tenants list
```

## Production database (Neon)

Use Neon's direct connection string as-is (`?sslmode=require&channel_binding=require`);
`core.db.url.normalize_db_url` converts it for asyncpg. See `docs/decisions.md` (ADR-001).
