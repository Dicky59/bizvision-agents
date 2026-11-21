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

## Knowledge base (ingestion & retrieval)

Needs `OPENAI_API_KEY` in `.env` (embeddings). Re-running any of these skips unchanged content.

```bash
uv run bv ingest bizvision --dry-run            # crawl + show extraction only (no embeddings, no DB writes)
uv run bv ingest bizvision                      # crawl the tenant's website and index it
uv run bv ingest bizvision --files docs/a.pdf --files docs/b.md   # index local files instead of crawling
uv run bv faq import bizvision faq/bizvision.yaml   # FAQ becomes exactly the entries in the file
uv run bv search bizvision "Paljonko AI-asiakaspalvelija maksaa?" --lang fi --top-k 5
```

## Chat with the assistant (terminal)

Needs `ANTHROPIC_API_KEY` and `OPENAI_API_KEY` in `.env` and an ingested tenant. Uses the same
graph, guards and persistence as the API will; empty line or `/quit` leaves and prints the session cost.

```bash
uv run bv chat bizvision                       # language detected per message
uv run bv chat bizvision --lang en --show-debug   # intent, search query, relevance, timings
```

## Production database (Neon)

Use Neon's direct connection string as-is (`?sslmode=require&channel_binding=require`);
`core.db.url.normalize_db_url` converts it for asyncpg. See `docs/decisions.md` (ADR-001).
