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

## Evals

```bash
uv run bv evals run bizvision --repeat 3                       # golden set, tenant's model
uv run bv evals run bizvision --model claude-haiku-5-5 --model claude-sonnet-5-5 --repeat 3
uv run bv evals run bizvision --only seo-fi --repeat 6         # a single case, repeated
```

Real API calls (Haiku run: about EUR 0.04; Sonnet about 20x). Prints pass rate, injection rate,
TTFT median/p90, cost, flaky cases and a min_relevance calibration table; exits 1 below target
(>= 90 % pass, 100 % injection). Cases live in `evals/<slug>.yaml`.

## Public API

```bash
uv run uvicorn api.main:app --reload --port 8000
```

| Endpoint | Purpose |
|---|---|
| `GET /v1/widget/{tenant}/config` | Public widget config (whitelisted fields) |
| `POST /v1/assistant/{tenant}/chat` | SSE stream: `meta`, `token`*, `sources`, `lead_form`, `done`, `error` |
| `POST /v1/assistant/{tenant}/leads` | Lead with explicit consent; the tenant is notified by email |
| `/v1/admin/...` | Tenants, ingest, files, FAQ, usage, leads (`Authorization: Bearer $ADMIN_API_KEY`) |
| `GET /widget.v1.js` | The built widget (`Cache-Control: public, max-age=86400`) |
| `GET /healthz` | Liveness |

Public routes require an `Origin` listed in the tenant's `allowed_origins`. Relevant env vars:
`RESEND_API_KEY`, `EMAIL_FROM` (sending subdomain, e.g. `BizVision AI <ai@notify.bizvision.fi>`),
`ADMIN_API_KEY` (admin routes answer 503 while it is empty), `TRUSTED_PROXY_COUNT` (1 behind Caddy).

```bash
uv run bv leads list bizvision                 # newest first; PENDING = notification not sent
uv run bv leads notify-pending bizvision       # retry failed notification emails
uv run bv leads delete bizvision a@example.com # erasure request (lead + its conversation)
uv run bv cleanup                              # retention: expired conversations and old leads
uv run bv usage bizvision --month 2026-11
uv run pytest -m live -k resend                # sends ONE real test email to the handoff address
```


## Widget

One script tag on the customer's site (Shadow DOM, no runtime dependencies, about 11 KB gzipped):

```html
<script src="https://agents.bizvision.fi/widget.v1.js" data-tenant="bizvision" defer></script>
```

Optional attributes: `data-lang="fi|en"` (default `<html lang>`), `data-position="bottom-left|bottom-right"`, `data-open="true"`, `data-api` (API base URL; default is the script's own origin).

```bash
cd widget && npm install
npm run dev                                   # test pages on http://localhost:5173 (needs the API on :8000)
npm run build                                 # -> widget/dist/widget.js (fails above 40 KB gzipped)
npm run typecheck && npm run lint && npm test # unit tests (SSE parser, Markdown/XSS, storage, colours)
npx playwright install                        # once: chromium, firefox, webkit
npm run test:e2e                              # 5 browser profiles against a mocked API
npm run a11y:lighthouse                       # accessibility >= 95 (dev server + API running)
LIVE=1 npx playwright test e2e/live.spec.ts --project=chromium   # real API and LLM, never sends a lead
```

The API serves the built file at `/widget.v1.js`; add the site to the tenant's `allowed_origins` (`http://localhost:5173` is there for the dev pages and must be removed before go-live).

## Production database (Neon)

Use Neon's direct connection string as-is (`?sslmode=require&channel_binding=require`);
`core.db.url.normalize_db_url` converts it for asyncpg. See `docs/decisions.md` (ADR-001).
