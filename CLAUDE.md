# CLAUDE.md — bizvision-agents

This file gives Claude Code the context it needs to work in this repository. Read it fully before starting any task, then read the relevant spec in `docs/specs/`.

## What this project is

`bizvision-agents` is the agent platform of **BizVision Oy** (bizvision.fi), a one-person AI automation consultancy serving Finnish SMEs. It hosts productized AI agents that are sold to customers as fixed-price packages with a monthly maintenance fee.

The platform is **multi-tenant**: each customer is a *tenant* with its own configuration, data and usage tracking. BizVision itself is tenant `bizvision`, and its own website is the first live demo.

Products built on the platform (in build order):

| Phase | Product | Folder | Status |
|---|---|---|---|
| 1 | Shared core + AI customer-service assistant (website chatbot) | `core/`, `agents/assistant/`, `widget/` | **In progress** |
| 2 | AI automation assessment tool (lead magnet on bizvision.fi/kartoitus) | `agents/assessment/` | Planned |
| 3 | Email agent (M365 / Gmail: classify, route, draft replies) | `agents/email/` | Planned |
| 4 | Reporting automation (data sources → AI-written periodic report) | `agents/reporting/` | Planned |

The marketing website (Next.js on Vercel) lives in a **separate repo** (`bizvision`). It only calls this platform's API and embeds the widget. Do not add website code here.

## Repository layout

```
bizvision-agents/
├── CLAUDE.md
├── README.md
├── pyproject.toml          # uv-managed Python project
├── docker-compose.yml      # local dev: postgres+pgvector, api
├── Dockerfile
├── .env.example
├── alembic/                # DB migrations
├── api/                    # FastAPI app: routers, dependencies, auth, rate limiting
├── core/                   # Shared, product-agnostic building blocks
│   ├── config.py           # pydantic-settings
│   ├── db/                 # SQLAlchemy models, session, repositories
│   ├── llm/                # LLM provider abstraction + usage/cost tracking
│   ├── embeddings/         # Embedding provider abstraction
│   ├── ingest/             # Crawler, file parsers, chunking
│   ├── rag/                # Hybrid retrieval (vector + full-text)
│   ├── notify/             # Email notifications (Resend)
│   └── tenants/            # Tenant config loading and validation
├── agents/
│   └── assistant/          # LangGraph graph, prompts, tools for the chatbot
│       └── prompts/        # Prompt templates as .md files
├── cli/                    # Typer CLI: `bv` admin commands
├── widget/                 # Embeddable chat widget (TypeScript + Vite)
├── workflows/              # n8n workflow JSON exports
├── evals/                  # Golden Q&A sets and eval runner
├── tests/
└── docs/
    ├── specs/              # Phase specifications (source of truth for scope)
    └── decisions.md        # Architecture decision log
```

## Tech stack

- **Python 3.12**, managed with **uv**
- **FastAPI** (async) + **Pydantic v2** + **pydantic-settings**
- **LangGraph** for agent orchestration (LangChain only where it saves real effort — prefer direct SDK calls)
- **PostgreSQL 16 + pgvector**, **SQLAlchemy 2.x (async)**, **Alembic** — local: Docker Compose; production: **Neon** (EU, Frankfurt), direct connection string (see ADR-001 in `docs/decisions.md`). Keep SQL portable Postgres: no Neon-specific features in application code.
- **LLM:** Anthropic Claude via the official `anthropic` SDK, behind `core/llm` abstraction
- **Embeddings:** provider abstraction in `core/embeddings`; default OpenAI `text-embedding-3-small` (1536 dims)
- **Crawling/parsing:** `httpx`, `trafilatura`, `pypdf`, `python-docx`
- **Email:** Resend
- **Widget:** TypeScript, Vite (library mode → single `widget.js`), no framework, Shadow DOM
- **Testing:** pytest, pytest-asyncio, respx (HTTP mocking)
- **Lint/format/type:** ruff (lint + format), pyright (basic mode)

Use the latest stable versions when adding dependencies. Do not add a new dependency without a clear reason; mention it in the commit message.

## Development environment

The developer works on **Windows 11 with Git Bash**. Keep commands and scripts bash-compatible and avoid Linux-only assumptions (e.g. no `make`-only workflows; paths via `pathlib`).

```bash
# First-time setup
cp .env.example .env            # fill in API keys
docker compose up -d db         # postgres + pgvector
uv sync
uv run alembic upgrade head

# Run
uv run uvicorn api.main:app --reload --port 8000

# Quality gates (run before every commit)
uv run ruff format .
uv run ruff check . --fix
uv run pyright
uv run pytest

# Admin CLI
uv run bv tenants create bizvision --config tenants/bizvision.yaml
uv run bv ingest bizvision
uv run bv chat bizvision          # interactive terminal chat for testing
uv run bv evals run bizvision

# Widget
cd widget && npm install && npm run dev     # dev server with test page
cd widget && npm run build                  # → widget/dist/widget.js
```

## Architecture rules

1. **Dependency direction:** `api/` and `agents/` depend on `core/`. `core/` never imports from `agents/` or `api/`. Agents never import from each other.
2. **Tenant isolation is non-negotiable.** Every query against tenant data filters by `tenant_id`. Repository functions take `tenant_id` as a required argument. Write a test for every new repository function that proves cross-tenant data is not returned.
3. **All LLM calls go through `core/llm`.** Never call the Anthropic SDK directly from agents. `core/llm` records a `usage_events` row (tenant, model, tokens, cost in EUR) for every call.
4. **Prompts live in files** (`agents/<name>/prompts/*.md`), not inline strings. Templates use simple `{placeholder}` formatting.
5. **Retrieved content is untrusted data.** Wrap it in clearly delimited tags in prompts and instruct the model to treat it as reference material, never as instructions.
6. **Async everywhere** in the request path. No blocking I/O in route handlers.
7. **Config over code:** per-tenant behaviour (tone, languages, handoff email, model, budgets) comes from tenant config, not `if tenant == ...` branches.
8. **Secrets** only via environment variables. Never commit `.env`, keys, or customer data. `tenants/*.yaml` may be committed only for the `bizvision` demo tenant.

## Privacy and data (GDPR)

- Customers are Finnish companies; assume EU data-residency expectations. Database and hosting are in the EU.
- **Never log personal data** (message contents, emails, names, phone numbers) at INFO level or above. Log IDs and metadata only.
- Conversations and messages have a configurable retention period (default 90 days); a scheduled cleanup job deletes expired rows.
- Lead capture requires an explicit consent checkbox; store the consent timestamp and the privacy-notice version.
- The LLM provider is behind an abstraction so a tenant can later be moved to an EU-hosted model endpoint if required.

## Language conventions

- **Code, comments, commit messages, docs in this repo:** English.
- **End-user-facing text** (widget UI strings, assistant default messages, emails to end users): **Finnish and English**, Finnish is the default. Keep strings in locale files (`widget/src/i18n/fi.json`, `en.json`; `core/notify/templates/{fi,en}/`).
- The assistant answers in the language the end user writes in.

## Coding conventions

- Type hints everywhere; Pydantic models for all API input/output.
- Small, focused modules. Prefer plain functions + dataclasses/Pydantic over class hierarchies.
- Errors: raise domain exceptions in `core/`, map them to HTTP errors in `api/`.
- Every feature ships with tests. External HTTP (LLM, embeddings, crawler targets, Resend) is mocked in unit tests; integration tests that hit real APIs are marked `@pytest.mark.live` and skipped by default.
- Database changes always via an Alembic migration.

## How to work in this repo

1. Read the active spec (`docs/specs/phase-1-core-and-assistant.md`) and work milestone by milestone, in order.
2. Before a milestone, briefly state the plan; after it, run the quality gates and summarise what changed.
3. Keep commits small and descriptive (Conventional Commits: `feat:`, `fix:`, `chore:`, `docs:`, `test:`).
4. When you make a non-obvious technical choice, add a dated entry to `docs/decisions.md` (context → decision → consequences).
5. If the spec is ambiguous or seems wrong, stop and ask rather than guessing on anything that affects data model, security or cost.
6. Do not start work from a later phase unless asked.

## Out of scope for now

- Admin web UI (admin is via CLI + admin API in phase 1)
- Billing/payments integration
- Multi-region deployment, Kubernetes
- Fine-tuning models
