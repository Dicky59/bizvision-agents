"""M3 live acceptance test: crawl the real bizvision.fi, embed with the real OpenAI API, and check
retrieval for six Finnish queries.

Skipped by default. Run it (with -s to see the calibration table):

    uv run pytest -m live -s tests/test_live_bizvision.py

THIS TEST DEPENDS ON THE LIVE bizvision.fi CONTENT. The expected URLs and the facts the queries
rely on describe the site as of 2026-11-20 (a bilingual site: Finnish pages under /fi, English
ones at the root, the Finnish free-audit page at /ai-audit, and an English-only mention of SEO on
/about). When pages are added, renamed or reworded, `CASES` below may need updating; a failure
here is first a reason to read the printed table, not necessarily a retrieval bug. The two
negative queries were chosen to stay out of scope permanently (accounting, weather).

Cost: about 12 pages (~30 chunks) plus 6 queries on text-embedding-3-small, i.e. cents. It writes
into the throwaway tenant from the `tenant_id` fixture in bv_test, never the dev database.

Two phases: right now the answerable queries only have to find their page within the default
top_k (6), and the off-topic query has to score below all of them. Once `min_relevance` has been
calibrated from the printed numbers (see ADR-004), tighten this test so that every answerable
query scores >= the threshold and the near-miss and the off-topic query score below it.
"""

import uuid
from dataclasses import dataclass

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.config import get_settings
from core.db.models import UsageEvent
from core.embeddings.client import EmbeddingClient
from core.embeddings.openai_provider import OpenAIEmbeddingProvider
from core.ingest import repository as repo
from core.ingest.crawler import make_client
from core.ingest.service import IngestStats, crawl_tenant
from core.rag.retrieve import RetrievedChunk, best_relevance, retrieve
from core.tenants.config import IngestConfig

pytestmark = pytest.mark.live
SM = async_sessionmaker[AsyncSession]

BASE = "https://www.bizvision.fi"
TOP_K = 6  # the default in tenants/bizvision.yaml (assistant.retrieval.top_k)


@dataclass(frozen=True)
class Case:
    query: str
    expected: tuple[str, ...]  # any of these URLs counts as a hit; empty = must not match well
    why: str


CASES = [
    Case(
        "Mitä palveluita tarjoatte?",
        (f"{BASE}/fi", f"{BASE}/fi/ai-automation", f"{BASE}/fi/digital-development"),
        "services: the 'mitä teemme' sections",
    ),
    Case(
        "Paljonko auditointi maksaa?",
        (f"{BASE}/ai-audit",),
        "pricing: 'Auditointi on ilmainen... saat selkeän hinnan ennen kuin mitään rakennetaan'",
    ),
    Case(
        "Mitä hyötyä tekoälyagenteista on?",
        (f"{BASE}/fi/ai-automation", f"{BASE}/fi"),
        "inflected form: 'tekoälyagenteista' stems to the same lexeme as 'tekoälyagentti/-agentit'",
    ),
    Case(
        "Teettekö myös hakukoneoptimointia?",
        (f"{BASE}/about",),
        "cross-language: only the ENGLISH About page mentions SEO; no word shared with the query",
    ),
    Case(
        "Hoidatteko yrityksen kirjanpidon?",
        (),
        "near-miss: business-services wording, but accounting is permanently out of scope",
    ),
    Case(
        "Millainen sää Helsingissä on huomenna?",
        (),
        "off-topic floor: mentions Helsinki, nothing else in common with the site",
    ),
]
ANSWERABLE = [c for c in CASES if c.expected]
OFF_TOPIC = CASES[-1]  # the floor; the accounting near-miss (CASES[-2]) is only reported


def short(url: str | None) -> str:
    return (url or "-").removeprefix(BASE) or "/"


def format_table(results: dict[str, list[RetrievedChunk]]) -> str:
    lines = ["", "query | first expected hit (rank) | best relevance | top 3 (relevance url)"]
    for case in CASES:
        chunks = results[case.query]
        rank = first_hit_rank(case, chunks)
        top3 = ", ".join(f"{c.relevance:.2f} {short(c.url or c.title)}" for c in chunks[:3])
        hit = "n/a (negative)" if not case.expected else (f"#{rank}" if rank else "MISS")
        lines.append(f"{case.query!r} | {hit} | {best_relevance(chunks):.3f} | {top3}")
        lines.append(f"    ({case.why})")
    return "\n".join(lines)


def first_hit_rank(case: Case, chunks: list[RetrievedChunk]) -> int | None:
    for i, chunk in enumerate(chunks, start=1):
        if chunk.url in case.expected:
            return i
    return None


async def embed_cost_rows(sm: SM, tenant_id: uuid.UUID) -> int:
    async with sm() as s:
        q = select(func.count()).select_from(UsageEvent).where(UsageEvent.tenant_id == tenant_id)
        return (await s.execute(q)).scalar_one()


async def crawl(sm: SM, embedder: EmbeddingClient, tenant_id: uuid.UUID) -> IngestStats:
    config = IngestConfig(start_urls=[BASE], use_sitemap=True, max_pages=50)
    async with make_client() as client:
        return await crawl_tenant(sm, embedder, tenant_id, config, client, delay=0.3)


async def test_live_bizvision_ingest_and_retrieval(sessionmaker: SM, tenant_id: uuid.UUID) -> None:
    settings = get_settings()
    if not settings.openai_api_key:
        pytest.skip("OPENAI_API_KEY not set")
    try:
        async with httpx.AsyncClient(timeout=10) as probe:
            await probe.get(BASE)
    except httpx.HTTPError:
        pytest.skip("bizvision.fi is not reachable")
    embedder = EmbeddingClient(
        OpenAIEmbeddingProvider(settings.openai_api_key), sessionmaker, settings.eur_per_usd
    )

    # ---- 1. first crawl indexes the site
    first = await crawl(sessionmaker, embedder, tenant_id)
    assert first.errors == 0, f"crawl errors: {first.error_samples}"
    assert first.new >= 10, f"expected about 12 pages, got {first.new}; has the site changed?"
    async with sessionmaker() as s:
        sources = {x.uri: x for x in await repo.list_sources(s, tenant_id, status="active")}
    expected_urls = {url for case in ANSWERABLE for url in case.expected}
    missing = sorted(expected_urls - set(sources))
    assert not missing, f"pages the test relies on are not indexed (site changed?): {missing}"
    # regression: /ai-audit is Finnish text served with <html lang="en">; the text must win
    assert sources[f"{BASE}/ai-audit"].language == "fi"

    # ---- 2. retrieval quality (all queries are Finnish)
    results: dict[str, list[RetrievedChunk]] = {}
    for case in CASES:
        async with sessionmaker() as s:
            results[case.query] = await retrieve(s, embedder, tenant_id, case.query, "fi", TOP_K)
    table = format_table(results)
    print(table)  # calibration data for min_relevance; visible with -s

    problems: list[str] = []
    for case in ANSWERABLE:
        if first_hit_rank(case, results[case.query]) is None:
            problems.append(
                f"{case.query!r}: none of {[short(u) for u in case.expected]} in top {TOP_K}"
            )
    weakest_answerable = min(best_relevance(results[c.query]) for c in ANSWERABLE)
    floor = best_relevance(results[OFF_TOPIC.query])
    if floor >= weakest_answerable:
        problems.append(
            f"off-topic query scored {floor:.3f}, not below the weakest answerable "
            f"query ({weakest_answerable:.3f})"
        )
    assert not problems, "\n".join(problems) + "\n" + table

    # ---- 3. re-running the crawl skips everything and costs nothing
    usage_before = await embed_cost_rows(sessionmaker, tenant_id)
    second = await crawl(sessionmaker, embedder, tenant_id)
    assert (second.new, second.updated, second.removed) == (0, 0, 0), second.to_dict()
    assert second.unchanged == first.new, (
        "pages changed between two runs seconds apart: extraction contains per-request content "
        f"(timestamps, build ids?) and would be re-embedded on every crawl. {second.to_dict()}"
    )
    # and the second crawl embedded nothing: no new usage rows
    assert await embed_cost_rows(sessionmaker, tenant_id) == usage_before
