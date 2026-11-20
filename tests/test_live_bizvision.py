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

Cost: about 12 pages (~30 chunks) plus 6 queries on text-embedding-3-small, i.e. cents. The site
is crawled and embedded once per module (fixture `live`). It writes into a throwaway tenant in
bv_test and deletes it afterwards, never touching the dev database.

Two phases: right now an answerable query only has to find its page within the default top_k (6),
and the off-topic query has to score below all of them. `min_relevance` is provisional (0.37 in
tenants/bizvision.yaml, from the six data points printed here); recalibrate it on the M4 evals and
then tighten this test so that answerable queries score >= the threshold and the negatives below.

Known miss (strict xfail): "Teettekö myös hakukoneoptimointia?". The only English-only mention of
SEO is one word inside a longer paragraph on /about, so neither the embedding (cross-language
synonym) nor the full-text leg (no IDF weighting) surfaces it. The strict xfail turns into a
failure the day it is fixed, as a reminder to remove the marker.
"""

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.config import get_settings
from core.db.models import Chunk, Source, Tenant, UsageEvent
from core.db.session import make_engine
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
SEO_MISS = (
    "cross-language synonym (hakukoneoptimointi ↔ SEO); "
    "expected fix: multilingual term expansion in M4 rewrite_query"
)


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


def first_hit_rank(case: Case, chunks: list[RetrievedChunk]) -> int | None:
    for i, chunk in enumerate(chunks, start=1):
        if chunk.url in case.expected:
            return i
    return None


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


async def usage_rows(sm: SM, tenant_id: uuid.UUID) -> int:
    async with sm() as s:
        q = select(func.count()).select_from(UsageEvent).where(UsageEvent.tenant_id == tenant_id)
        return (await s.execute(q)).scalar_one()


async def crawl(sm: SM, embedder: EmbeddingClient, tenant_id: uuid.UUID) -> IngestStats:
    config = IngestConfig(start_urls=[BASE], use_sitemap=True, max_pages=50)
    async with make_client() as client:
        return await crawl_tenant(sm, embedder, tenant_id, config, client, delay=0.3)


@dataclass
class Live:
    sessionmaker: SM
    embedder: EmbeddingClient
    tenant_id: uuid.UUID
    first: IngestStats
    token_counts: list[int]
    sources: dict[str | None, Source]
    results: dict[str, list[RetrievedChunk]]


@pytest_asyncio.fixture(scope="module")
async def live(migrated_db: None) -> AsyncIterator[Live]:
    """Crawl + embed bizvision.fi once, then run all six queries (printed as a table)."""
    settings = get_settings()
    if not settings.openai_api_key:
        pytest.skip("OPENAI_API_KEY not set")
    try:
        async with httpx.AsyncClient(timeout=10) as probe:
            await probe.get(BASE)
    except httpx.HTTPError:
        pytest.skip("bizvision.fi is not reachable")

    engine = make_engine(settings.test_database_url)
    sm: SM = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id = uuid.uuid4()
    async with sm() as s:
        s.add(
            Tenant(
                id=tenant_id,
                slug=f"live-{tenant_id.hex[:8]}",
                name="Live test",
                public_key=f"pk_{tenant_id.hex}",
            )
        )
        await s.commit()
    try:
        embedder = EmbeddingClient(
            OpenAIEmbeddingProvider(settings.openai_api_key), sm, settings.eur_per_usd
        )
        first = await crawl(sm, embedder, tenant_id)
        async with sm() as s:
            sources = {x.uri: x for x in await repo.list_sources(s, tenant_id, status="active")}
            token_counts = list(
                (await s.execute(select(Chunk.token_count).where(Chunk.tenant_id == tenant_id)))
                .scalars()
                .all()
            )
        results: dict[str, list[RetrievedChunk]] = {}
        for case in CASES:
            async with sm() as s:
                results[case.query] = await retrieve(
                    s, embedder, tenant_id, case.query, "fi", TOP_K
                )

        # calibration data for min_relevance; visible with -s
        print(format_table(results))
        print(
            f"\npages: {first.new}  chunks: {first.chunks}  "
            f"avg tokens/chunk: {sum(token_counts) / max(1, len(token_counts)):.0f}  "
            f"max: {max(token_counts, default=0)}"
        )
        yield Live(sm, embedder, tenant_id, first, token_counts, sources, results)
    finally:
        async with sm() as s:
            await s.execute(delete(Tenant).where(Tenant.id == tenant_id))
            await s.commit()
        await engine.dispose()


async def test_crawl_indexes_the_site(live: Live) -> None:
    assert live.first.errors == 0, f"crawl errors: {live.first.error_samples}"
    assert live.first.new >= 10, f"expected about 12 pages, got {live.first.new}; site changed?"
    expected_urls = {url for case in ANSWERABLE for url in case.expected}
    missing = sorted(expected_urls - set(live.sources))
    assert not missing, f"pages the test relies on are not indexed (site changed?): {missing}"


def test_wrongly_labelled_page_is_detected_as_finnish(live: Live) -> None:
    # /ai-audit is Finnish text served with <html lang="en">; the text must win
    assert live.sources[f"{BASE}/ai-audit"].language == "fi"


def test_chunks_are_reasonably_sized(live: Live) -> None:
    counts = live.token_counts
    assert max(counts) <= 800, "a chunk exceeds the 800-token maximum"
    tiny = sum(1 for c in counts if c < 40)
    assert tiny <= max(2, len(counts) // 10), f"{tiny} of {len(counts)} chunks are under 40 tokens"


@pytest.mark.parametrize(
    "case",
    [
        pytest.param(c, id=c.query)
        if "hakukone" not in c.query
        else pytest.param(c, id=c.query, marks=pytest.mark.xfail(reason=SEO_MISS, strict=True))
        for c in ANSWERABLE
    ],
)
def test_answerable_query_finds_its_page(live: Live, case: Case) -> None:
    chunks = live.results[case.query]
    rank = first_hit_rank(case, chunks)
    assert rank is not None, (
        f"none of {[short(u) for u in case.expected]} in top {TOP_K}: "
        f"{[short(c.url) for c in chunks]}"
    )


def test_off_topic_scores_below_every_answerable_query(live: Live) -> None:
    floor = best_relevance(live.results[OFF_TOPIC.query])
    weakest = min(best_relevance(live.results[c.query]) for c in ANSWERABLE)
    assert floor < weakest, f"off-topic scored {floor:.3f}, weakest answerable {weakest:.3f}"


async def test_recrawl_skips_everything_and_costs_nothing(live: Live) -> None:
    before = await usage_rows(live.sessionmaker, live.tenant_id)
    second = await crawl(live.sessionmaker, live.embedder, live.tenant_id)
    assert (second.new, second.updated, second.removed) == (0, 0, 0), second.to_dict()
    assert second.unchanged == live.first.new, (
        "pages changed between two runs seconds apart: extraction contains per-request content "
        f"(timestamps, build ids?) and would be re-embedded on every crawl. {second.to_dict()}"
    )
    assert await usage_rows(live.sessionmaker, live.tenant_id) == before  # nothing was embedded
