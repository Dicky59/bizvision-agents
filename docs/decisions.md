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
- **`usage_events.cost_eur` is `Numeric(18, 10)`** (migration `0003`; was `Numeric(12, 6)`). Found by the live embeddings test: 10 tokens of `text-embedding-3-small` cost about 1.8e-7 EUR and rounded to 0 at 6 decimals, so every short chat-turn query embedding would have been recorded as free. `core/llm/pricing.cost_eur` now rounds to 10 decimals. Monthly budget sums are unaffected in behaviour (covered by a test summing 1000 tiny rows).


---

## ADR-004 — M3: ingestion and retrieval

**Date:** 2026-11-20
**Status:** Accepted

### Context
M3 turns a tenant's website, files and FAQ into searchable chunks. Running the crawler against bizvision.fi surfaced several real-world issues that shaped the decisions below.

### Decision
- **Source identity.** Web pages and FAQ entries are keyed by `uri`, files by `filename`. Migration `0004` adds the partial unique index `uq_sources_identity` on `(tenant_id, type, coalesce(uri, filename))`. Removed sources keep their row, so a page that comes back is reactivated, not duplicated. Linked PDFs found by the crawler are `type=web` sources (uri = the PDF URL), so the "page disappeared" logic covers them uniformly.
- **URL normalisation.** Scheme/host lowercased, fragment and default port dropped, trailing slash stripped (not on the root). A same-domain `<link rel="canonical">` becomes the source identity and deduplicates pages reachable under several URLs; a cross-domain canonical is ignored. `www.example.com` and `example.com` count as the same site.
- **Extraction.** trafilatura with `favor_recall=True` **and** `include_formatting=True`. The latter is what produces `#` headings and lists; without it heading-aware chunking sees flat text. Landing pages with short service/pricing blocks are kept. `bv ingest <tenant> --dry-run` prints per-page extraction results (no embeddings, no DB writes) so output can be reviewed before indexing.
- **The crawler is stateless: all cookies are blocked.** bizvision.fi remembers a locale after a visit to `/fi/...`; with a cookie jar, later English pages came back as Finnish and were dropped as canonical duplicates (10 of 12 sitemap pages crawled, no error reported). Found via `--dry-run`.
- **Language detection.** `<html lang>` is trusted unless the text is *confidently* the other language (≥ 20 words, ≥ 5 stopword hits, ≥ 3× the other language's hits); then the text wins and a debug line is logged. Reason: bizvision.fi serves a Finnish page with `lang="en"`, and the language drives the full-text config and the retrieval language boost. A small stopword heuristic (fi/en) is used instead of a dependency.
- **SSRF guard** (`core/ingest/safety.py`), because phase 2 will crawl user-supplied URLs with the same crawler: http/https only; the host must resolve only to public addresses (private, loopback, link-local, multicast, reserved, unspecified; IPv4-mapped IPv6 unwrapped); every redirect hop is re-validated (redirects are followed manually, max 5); response bodies are capped at 5 MB while streaming. Residual risk: DNS rebinding. We validate the resolved addresses and httpx resolves again when connecting, so an attacker-controlled DNS server could answer differently the second time. Closing it needs pinning the connection to the validated IP; to be done before phase 2 accepts arbitrary URLs.
- **Sitemaps** containing a DTD or entity declaration are rejected (XML bombs) instead of adding `defusedxml`. robots.txt: 4xx means allowed, 5xx means disallow-all (RFC 9309).
- **Transactions.** Embedding happens *before* the write transaction; one page is one transaction (source upsert + chunk replacement). A failed embedding leaves the previous chunks and hash untouched, so the next run retries.
- **Removal is conservative.** A source is marked `removed` (chunks deleted) only when a *complete* crawl no longer finds it. Nothing is removed when the crawl produced no pages (outage, robots 5xx) or was truncated by `max_pages`. Only HTTP 404/410 count as "gone"; other failures keep the existing content.
- **`max_pages` limitation.** When a crawl is truncated by `ingest.max_pages`, pages that were removed from the site **cannot be detected** (the crawler cannot tell "not reached" from "gone"), so stale chunks stay searchable until the limit is raised or the crawl completes. `bv ingest` prints `crawl truncated at N pages — removed pages won't be detected; consider raising ingest.max_pages` (also stored as `removal_skipped` in the job stats).
- **Finnish full-text search.** Postgres' snowball `finnish` config stems inflections but does **not split compounds** ("asiakaspalvelu" does not match "palvelu"). Accepted: vector search covers compounds, and RRF fusion combines both. No test assumes compound matching. The config name comes from a fixed whitelist (`fi`→`finnish`, `en`→`english`, else `simple`) and is bound as a `regconfig` parameter, never interpolated.
- **FAQ boost needs no column.** FAQ chunks are identified at retrieval time by joining `sources.type = 'faq'`. FAQ entries are one chunk each, keyed by a hash of the normalised question; entries missing from a new import are deleted.
- **Embedding costs are recorded but never blocked** by the LLM budget guard (the spec guards LLM calls only).
- **Retrieval: the full-text leg uses an OR query, not `websearch_to_tsquery`** (deviation from spec §6.5). `websearch_to_tsquery` ANDs every term: "Paljonko AI-asiakaspalvelija maksaa?" became `palj & ai-asiakaspalvelij & … & maks`, which only matches chunks containing all of those words, so for natural questions the full-text leg almost never fired and hybrid search degraded to vector-only. `core/rag/query.py` instead tokenises the question into letters/digits (max 12 distinct terms, so no tsquery operators can get through), joins them with `|`, and `ts_rank_cd` ranks chunks that match more terms higher. Inflections still work through the snowball stemmer (hinta/hinnat/hinnasta, palvelu/palvelut/palveluita); compounds and some stem mismatches (yrityksille/yritys) do not, and are left to the vector leg.
- **Retrieval: fusion and boosts.** Vector top 20 (cosine) and full-text top 20, fused with RRF (k = 60). Then two multiplicative boosts on the fused score: FAQ chunks ×1.15, and chunks whose language equals the query language ×1.2. The language boost is soft on purpose (no filter), so other-language content is still found when nothing matches the query language, while translated duplicates (bizvision.fi serves every page in fi and en) no longer crowd out `top_k`. RRF scores are close together (rank 1 vs rank 2 differ by about 1.6 %), so both boosts effectively decide ordering among near-equal candidates; revisit the factors against the eval set in M4.
- **Retrieval: `relevance` is per chunk.** Each returned chunk carries its own cosine similarity to the query, clamped to 0..1, also for chunks found only by the full-text leg (the similarity is computed in the same SQL). `best_relevance(chunks)` is the confidence signal for the "I don't know" path. Ordering uses the fused score, which is not comparable across queries. The spec's `min_relevance: 0.35` is not yet calibrated: `text-embedding-3-small` similarities for good Finnish matches are often 0.3–0.6, so tune it on the real sample queries before M4 relies on it.
- **Retrieval: HNSW and tenant filters.** Each retrieval transaction sets `hnsw.iterative_scan = strict_order` (pgvector ≥ 0.8; local image 0.8.7). Without it, an HNSW scan that is filtered by `tenant_id` afterwards can return fewer than the requested rows when a tenant is a small share of a large table. Neon provides pgvector 0.8; older versions would fail on the `SET LOCAL`.

### Consequences
- Re-running `bv ingest` is cheap: unchanged pages cost no embedding call and no `usage_events` row.
- A very large site needs `ingest.max_pages` raised for removal detection to work.
- The DNS-rebinding gap is documented and must be closed before untrusted URLs are crawled.
- Changing the crawler's identity rules (normalisation/canonical) later would orphan existing source rows; a reindex handles it.
