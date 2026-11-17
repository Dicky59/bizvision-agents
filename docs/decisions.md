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
