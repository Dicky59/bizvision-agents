# Architecture decision log

Short, dated records of non-obvious technical choices. Newest at the bottom.
Format: **Context → Decision → Consequences**. Superseded decisions are kept and marked, not deleted.

---

## ADR-001 — Production database: Neon (managed Postgres, EU)

**Date:** 2026-11-17
**Status:** Accepted

### Context
The platform needs PostgreSQL with `pgvector`, hosted in the EU, for a one-person company that wants minimal operations work. Candidates: Neon, Supabase, self-hosted Postgres on the API VM.

The application already provides its own API (FastAPI), data access (SQLAlchemy + Alembic) and authentication (admin API key, tenant origin checks). Supabase's main value — Auth, Storage, Realtime, auto-generated REST — would go unused in phase 1. Self-hosting is cheapest but makes backups, upgrades and recovery the developer's responsibility.

### Decision
- **Production:** Neon, EU region (Frankfurt), paid plan with scale-to-zero disabled on the production branch.
- **Local development and tests:** Postgres 16 + pgvector in Docker Compose.
- The API connects with Neon's **direct (non-pooled)** connection string and its own SQLAlchemy pool; Alembic also uses the direct connection.
- Neon branches: `main` (production), `dev` (shared development), short-lived branches for testing risky migrations against a copy of production data.
- Independent weekly `pg_dump` to EU object storage in addition to Neon's point-in-time restore.

### Consequences
- No database server to operate; the API VM only runs `api` + `caddy`.
- Branching gives cheap, realistic migration testing.
- If the pooled endpoint is ever used, asyncpg statement caching must be disabled (`statement_cache_size=0`, `prepared_statement_cache_size=0`).
- Application SQL stays portable Postgres, so moving to Supabase or self-hosted Postgres later only means changing connection strings and running migrations.
- Revisit if a customer portal with end-user login is built — Supabase Auth (or a dedicated auth provider) may then be worth adding; that does not require moving the database.

---

## ADR-002 — M1 bootstrap choices

**Date:** 2026-11-17
**Status:** Accepted

### Context
M1 fixes schema conventions that later milestones build on.

### Decision
- Primary keys are UUID v4 (`uuid.uuid4`, stdlib); the spec allows v4 or v7 and v4 needs no dependency.
- "Enum" columns (`status`, `type`, `kind`, `role`) are strings with CHECK constraints, not Postgres enum types, so adding a value is a plain migration.
- `chunks.tsv` is a plain `tsvector` column filled at ingest time, because the text-search config depends on each chunk's language (`finnish`/`english`/`simple`); a generated column cannot do that.
- Heavy dependencies (anthropic, openai, langgraph, trafilatura, ...) are added in the milestone that uses them.
- Neon URLs (`sslmode=require&channel_binding=require`) are converted for asyncpg by `core.db.url.normalize_db_url`, used by both the app engine and Alembic.
- Tests run against a separate `bv_test` database in the same Postgres container (created by an init script), not a schema. Alembic honours `ALEMBIC_DATABASE_URL` so the migration test can target it.

### Consequences
- Changing the embedding dimension (1536) needs a new migration plus reindex.
- Init scripts only run on a fresh volume; documented in the README.

---

## ADR-003 — M2: LLM and embeddings core

**Date:** 2026-11-19
**Status:** Accepted

### Context
M2 introduces the only code path to external model providers. Several details of the current SDKs/models differ from what the spec assumed.

### Decision
- **Single call path.** `LLMClient` (`core/llm/client.py`) does budget check → provider → `usage_events` row; `EmbeddingClient` does provider → row. Agents never touch a provider directly (rule 3).
- **Usage rows use their own short DB session**, so a failing or cancelled request cannot lose cost data. Streaming writes the row in a `finally` (shielded from cancellation).
- **Early-closed streams.** Anthropic reports final output tokens only in the last `message_delta`. If the consumer closes the stream first, output tokens are estimated as `chars / 4` and the row gets `estimated = true`. Chosen over a JSONB note field: a boolean column is queryable (`WHERE estimated`) and needs no JSON handling. Added by migration `0002` (`usage_events.estimated boolean NOT NULL DEFAULT false`). Input tokens are exact (reported in `message_start`).
- **Retries are ours, not the SDK's** (`max_retries=0`, `core/retry.py`, shared by LLM and embeddings): 429, 529 and 5xx plus connection errors/timeouts, max 3 retries, `retry-after` honoured and capped at 20 s, otherwise `1s * 2^n + jitter` (also capped). For streams only stream *establishment* is retried; errors after events started propagate.
- **No `temperature` is sent.** The current SDK has no sampling parameters and the API rejects them for the 5.5 models. `temperature` stays in the `LLMProvider` signature and tenant config (spec) but is ignored; revisit if sampling control returns. (Tenant config field kept so YAML stays stable.)
- **Thinking is switched off per model for chat** via a field in the pricing table (`claude-haiku-5-5`: `disabled`; `claude-sonnet-5-5`: `between_tools`, since `disabled` is a 400 there). Otherwise adaptive thinking is on by default and would consume the 600-token output cap. Unknown models send no `thinking` parameter. Verify with the `@live` test when keys are available.
- **Prompt caching:** `cache_control: ephemeral` on the static system block. It silently does nothing below the model's minimum cacheable length; tests assert the request carries it and that cache tokens are priced, never that cache hits occur.
- **Pricing table is hand-maintained** (`core/llm/pricing.py`, prices as of 2026-11-03). Cache-read for `claude-haiku-5-5` is assumed 0.1× input (not verified). Haiku 5.5 is priced higher above 100K-token prompts; irrelevant for our prompt sizes. Unknown model → warning + cost 0.
- **Budget guard** sums the current UTC calendar month per tenant, cached 60 s per process (fine for a single instance). The check runs before a call starts, so one in-flight answer can overshoot slightly.
- **Embedding client signature** differs slightly from the spec: the provider returns an `EmbeddingResult` (vectors + token count) and `EmbeddingClient.embed(tenant_id, texts)` records usage, because the provider has no tenant context.
- **Tests mock HTTP with `httpx2.MockTransport`, not respx.** Both SDKs now use `httpx2`, which respx cannot intercept; the transport is injected via the SDKs' `http_client` argument.
- **Live tests** use a throwaway tenant in `bv_test` (fixture `tenant_id`), never the dev database.

### Consequences
- Changing the embedding dimension still needs a migration + reindex (unchanged from ADR-002).
- `usage_events.input_tokens` includes cache read/write tokens so totals add up; cost uses the per-class prices.
- Cost reports can distinguish exact from estimated rows via `estimated`.
