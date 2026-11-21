import uuid

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.db.models import Source, UsageEvent
from core.ingest import repository as repo
from core.ingest.chunking import ChunkDraft
from core.rag.query import build_or_query
from core.rag.retrieve import (
    RetrievedChunk,
    best_relevance,
    fulltext_search,
    retrieve,
    vector_search,
)
from core.rag.rrf import fuse
from tests.fakes import (
    FakeEmbeddingProvider,
    axis_vector,
    make_embedder,
    tilted_vector,
    topic_vector,
)

SM = async_sessionmaker[AsyncSession]


def embedder_for(sm: SM):
    return make_embedder(sm, FakeEmbeddingProvider(vector_for=topic_vector))


async def add(
    sm: SM,
    tenant_id: uuid.UUID,
    key: str,
    content: str,
    *,
    language: str | None = "fi",
    type_: str = "web",
    title: str | None = "Sivu",
    vector: list[float] | None = None,
) -> uuid.UUID:
    async with sm() as s:
        src = await repo.upsert_source(
            s, tenant_id, type_=type_, key=key, title=title, language=language, content_hash=key
        )
        await repo.replace_chunks(
            s,
            tenant_id,
            src.id,
            title=title,
            language=language,
            drafts=[ChunkDraft(0, content, "Otsikko", len(content) // 4)],
            vectors=[vector or topic_vector(content)],
        )
        await s.commit()
        return src.id


async def search(
    sm: SM,
    tenant_id: uuid.UUID,
    query: str,
    *,
    language: str | None = "fi",
    top_k: int = 5,
) -> list[RetrievedChunk]:
    embedder, _ = embedder_for(sm)
    async with sm() as s:
        return await retrieve(s, embedder, tenant_id, query, language, top_k)


# ------------------------------------------------------------------ pure units


def test_rrf_scores_and_order() -> None:
    scores = fuse([["a", "b", "c"], ["b", "d"]])
    assert scores["b"] == pytest.approx(1 / 62 + 1 / 61)
    assert scores["a"] == pytest.approx(1 / 61)
    assert max(scores, key=lambda k: scores[k]) == "b"  # found by both legs
    assert fuse([]) == {} and fuse([[]]) == {}
    assert fuse([["x"]], k=0)["x"] == 1.0


def test_rrf_weights_scale_each_ranking() -> None:
    scores = fuse([["a", "b"], ["b"]], weights=[0.5, 1.0])
    assert scores["a"] == pytest.approx(0.5 / 61)
    assert scores["b"] == pytest.approx(0.5 / 62 + 1 / 61)
    with pytest.raises(ValueError):
        fuse([["a"]], weights=[1.0, 1.0])


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Paljonko palvelu maksaa?", "paljonko | palvelu | maksaa"),
        ("hinta hinta HINTA", "hinta"),
        ("AI-asiakaspalvelija", "ai | asiakaspalvelija"),
        ("Mitä ääni maksaa öisin", "mitä | ääni | maksaa | öisin"),
        ("a b c", None),  # single characters dropped
        ("", None),
        ("?!&|:()<->'\"", None),
    ],
)
def test_build_or_query(raw: str, expected: str | None) -> None:
    assert build_or_query(raw) == expected


def test_build_or_query_never_emits_operators_and_caps_terms() -> None:
    q = build_or_query("hinta & !palvelu | (x:*) <-> 'y' ; drop table")
    assert q is not None
    assert not set("&!()':*<>;") & set(q)
    assert q.count("|") + 1 <= 12
    many = build_or_query(" ".join(f"sana{i}" for i in range(50)))
    assert many is not None and many.count("|") == 11


# ------------------------------------------------------------------ search legs


async def test_vector_leg_ranks_by_cosine_similarity(
    sessionmaker: SM, tenant_id: uuid.UUID
) -> None:
    await add(sessionmaker, tenant_id, "u1", "Hinnat alkavat 990 eurosta")
    await add(sessionmaker, tenant_id, "u2", "Yhteystiedot löytyvät alta")
    async with sessionmaker() as s:
        hits = await vector_search(s, tenant_id, axis_vector(0))
    assert [h.chunk.content for h in hits][0].startswith("Hinnat")
    assert hits[0].similarity == pytest.approx(1.0) and hits[1].similarity == pytest.approx(0.0)


@pytest.mark.parametrize("query", ["hinta", "hinnasta", "Hinnat"])
async def test_fulltext_leg_matches_finnish_inflections(
    sessionmaker: SM, tenant_id: uuid.UUID, query: str
) -> None:
    await add(sessionmaker, tenant_id, "u1", "Hinnat alkavat 990 eurosta")
    await add(sessionmaker, tenant_id, "u2", "Yhteystiedot löytyvät alta")
    async with sessionmaker() as s:
        hits = await fulltext_search(s, tenant_id, query, "fi", axis_vector(5))
    assert [h.chunk.content for h in hits] == ["Hinnat alkavat 990 eurosta"]


async def test_fulltext_leg_finds_chunks_for_natural_questions(
    sessionmaker: SM, tenant_id: uuid.UUID
) -> None:
    """An AND query would need 'paljonko' and 'maksaa' in the chunk too; OR needs only one term."""
    await add(sessionmaker, tenant_id, "u1", "Palvelut sopivat pienille yrityksille")
    async with sessionmaker() as s:
        hits = await fulltext_search(s, tenant_id, "Paljonko palvelu maksaa?", "fi", axis_vector(5))
    assert len(hits) == 1


async def test_fulltext_leg_is_injection_safe_and_handles_stopword_queries(
    sessionmaker: SM, tenant_id: uuid.UUID
) -> None:
    await add(sessionmaker, tenant_id, "u1", "Hinnat alkavat 990 eurosta")
    async with sessionmaker() as s:
        hits = await fulltext_search(s, tenant_id, "hinta & | ! ( ) : <-> '", "fi", axis_vector(5))
        assert len(hits) == 1
        assert await fulltext_search(s, tenant_id, "ja", "fi", axis_vector(5)) == []  # stop word
        assert await fulltext_search(s, tenant_id, "?!", "fi", axis_vector(5)) == []


# ------------------------------------------------------------------ fused retrieval


async def test_retrieve_returns_best_first_with_metadata(
    sessionmaker: SM, tenant_id: uuid.UUID
) -> None:
    await add(
        sessionmaker, tenant_id, "https://x.fi/hinnat", "Hinnat alkavat 990 eurosta", title="Hinnat"
    )
    await add(sessionmaker, tenant_id, "https://x.fi/yhteys", "Yhteystiedot löytyvät alta")
    await add(sessionmaker, tenant_id, "esite.pdf", "Palvelut sopivat yrityksille", type_="file")

    results = await search(sessionmaker, tenant_id, "Paljonko se maksaa?", top_k=3)

    first = results[0]
    assert first.content == "Hinnat alkavat 990 eurosta"
    assert (first.url, first.title, first.source_type, first.heading_path) == (
        "https://x.fi/hinnat",
        "Hinnat",
        "web",
        "Otsikko",
    )
    assert first.relevance == pytest.approx(1.0) and not first.is_faq
    assert [r.score for r in results] == sorted((r.score for r in results), reverse=True)
    assert {r.url for r in results if r.source_type == "file"} == {None}  # files have no URL
    assert best_relevance(results) == pytest.approx(1.0)


async def test_keyword_match_lifts_a_chunk_above_a_pure_vector_neighbour(
    sessionmaker: SM, tenant_id: uuid.UUID
) -> None:
    # the query "palvelu" embeds on axis 1: 'near' is the closest vector (rank 1 in the vector
    # leg) but shares no word with it; 'kw' is vector rank 2 but the only full-text hit
    await add(sessionmaker, tenant_id, "near", "Jotain aivan muuta", vector=axis_vector(1))
    await add(sessionmaker, tenant_id, "kw", "Palvelut yrityksille", vector=axis_vector(7))
    results = await search(sessionmaker, tenant_id, "palvelu", top_k=2)
    assert [r.content for r in results] == ["Palvelut yrityksille", "Jotain aivan muuta"]
    assert results[0].relevance == 0.0 and results[1].relevance == pytest.approx(1.0)


async def test_relevance_is_clamped_and_best_relevance_handles_empty(
    sessionmaker: SM, tenant_id: uuid.UUID
) -> None:
    await add(sessionmaker, tenant_id, "opp", "Vastakkainen", vector=axis_vector(0, -1.0))
    results = await search(sessionmaker, tenant_id, "paljon", top_k=1)
    assert results[0].relevance == 0.0  # cosine similarity -1 is clamped into 0..1
    assert best_relevance([]) == 0.0


async def test_top_k_and_blank_queries(sessionmaker: SM, tenant_id: uuid.UUID) -> None:
    for i in range(4):
        await add(sessionmaker, tenant_id, f"u{i}", f"Hinnat versio {i}")
    assert len(await search(sessionmaker, tenant_id, "hinta", top_k=2)) == 2
    embedder, provider = embedder_for(sessionmaker)
    async with sessionmaker() as s:
        assert await retrieve(s, embedder, tenant_id, "   ", "fi", 5) == []
        assert await retrieve(s, embedder, tenant_id, "hinta", "fi", 0) == []
    assert provider.calls == []  # no embedding cost for nothing


async def test_retrieve_records_one_embed_usage_row(sessionmaker: SM, tenant_id: uuid.UUID) -> None:
    await add(sessionmaker, tenant_id, "u1", "Hinnat alkavat 990 eurosta")
    await search(sessionmaker, tenant_id, "hinta")
    async with sessionmaker() as s:
        rows = (
            (await s.execute(select(UsageEvent).where(UsageEvent.tenant_id == tenant_id)))
            .scalars()
            .all()
        )
    assert [(r.operation, r.provider) for r in rows] == [("embed", "fake")]


async def test_faq_chunks_get_a_boost(sessionmaker: SM, tenant_id: uuid.UUID) -> None:
    for i in range(3):  # three web chunks that are slightly *better* vector matches than the FAQ
        await add(
            sessionmaker,
            tenant_id,
            f"https://x.fi/{i}",
            f"Alkaen {i}90 euroa",
            vector=axis_vector(0),
        )
    await add(  # no word shared with the query and similarity 0.99: only the boost can lift it
        sessionmaker,
        tenant_id,
        "faq:1",
        "Montako euroa?\n\nAlkaen 990 euroa",
        type_="faq",
        title="Montako euroa?",
        vector=tilted_vector(0.99),
    )
    results = await search(sessionmaker, tenant_id, "kuinka paljon", top_k=4)
    assert results[0].is_faq and results[0].url is None and results[0].source_type == "faq"
    assert results[0].relevance == pytest.approx(0.99)  # boost changes order, not relevance
    assert not any(r.is_faq for r in results[1:])


async def _add_translated_pair(sm: SM, tenant_id: uuid.UUID, *, better: str) -> None:
    """fi/en duplicates with no keyword overlap with the queries; `better` has similarity 1.0,
    the other 0.99, so without a language boost the `better` one always ranks first."""
    for lang, key, text in (
        ("fi", "https://x.fi/fi", "Alkaen 990 euroa kuukaudessa"),
        ("en", "https://x.fi/en", "Starting from 990 euros monthly"),
    ):
        vec = axis_vector(0) if lang == better else tilted_vector(0.99)
        await add(sm, tenant_id, key, text, language=lang, vector=vec)


async def test_same_language_chunks_are_preferred_but_not_required(
    sessionmaker: SM, tenant_id: uuid.UUID, other_tenant_id: uuid.UUID
) -> None:
    # Finnish visitor: the English duplicate is the (slightly) closer vector, yet Finnish wins
    await _add_translated_pair(sessionmaker, tenant_id, better="en")
    fi = await search(sessionmaker, tenant_id, "kuinka paljon", language="fi", top_k=1)
    assert [r.language for r in fi] == ["fi"]  # top_k=1: the duplicate does not crowd it out
    both = await search(sessionmaker, tenant_id, "kuinka paljon", language="fi", top_k=5)
    assert [r.language for r in both] == ["fi", "en"]  # soft preference: English still returned

    # English visitor, mirrored
    await _add_translated_pair(sessionmaker, other_tenant_id, better="fi")
    en = await search(sessionmaker, other_tenant_id, "how much", language="en", top_k=1)
    assert [r.language for r in en] == ["en"]

    # no language given: no preference, closest vector wins
    none = await search(sessionmaker, other_tenant_id, "how much", language=None, top_k=1)
    assert [r.language for r in none] == ["fi"]


async def test_other_language_content_is_found_when_nothing_matches_the_query_language(
    sessionmaker: SM, tenant_id: uuid.UUID
) -> None:
    await add(
        sessionmaker, tenant_id, "https://x.fi/en", "Prices start at 990 euros", language="en"
    )
    results = await search(sessionmaker, tenant_id, "Paljonko se maksaa?", language="fi")
    assert [r.content for r in results] == ["Prices start at 990 euros"]
    assert results[0].relevance == pytest.approx(1.0)


async def test_inactive_sources_are_never_returned(sessionmaker: SM, tenant_id: uuid.UUID) -> None:
    sid = await add(sessionmaker, tenant_id, "u1", "Hinnat alkavat 990 eurosta")
    await add(sessionmaker, tenant_id, "u2", "Hinnat ovat voimassa toistaiseksi")
    async with sessionmaker() as s:  # status 'error' keeps the chunks but must hide them
        await s.execute(update(Source).where(Source.id == sid).values(status="error"))
        await s.commit()
    assert [r.content for r in await search(sessionmaker, tenant_id, "hinta")] == [
        "Hinnat ovat voimassa toistaiseksi"
    ]
    async with sessionmaker() as s:
        await repo.mark_removed(s, tenant_id, [sid])
        await s.commit()
    assert len(await search(sessionmaker, tenant_id, "hinta")) == 1


async def test_empty_tenant_returns_nothing(sessionmaker: SM, tenant_id: uuid.UUID) -> None:
    assert await search(sessionmaker, tenant_id, "hinta") == []


# ------------------------------------------------------------------ tenant isolation


async def test_cross_tenant_data_is_never_returned(
    sessionmaker: SM, tenant_id: uuid.UUID, other_tenant_id: uuid.UUID
) -> None:
    await add(sessionmaker, tenant_id, "https://a.fi/hinnat", "Salainen hinnasto 990 euroa")
    await add(sessionmaker, other_tenant_id, "https://b.fi/yhteys", "Yhteystiedot löytyvät alta")

    # fused retrieval, both legs, and the raw legs: B never sees A's chunk, even for A's own words
    for query in ("hinta", "Salainen hinnasto 990 euroa", "paljon"):
        assert all(
            "Salainen" not in r.content for r in await search(sessionmaker, other_tenant_id, query)
        )
    async with sessionmaker() as s:
        vec = await vector_search(s, other_tenant_id, axis_vector(0))
        fts = await fulltext_search(s, other_tenant_id, "salainen hinnasto", "fi", axis_vector(0))
        assert all(h.chunk.tenant_id == other_tenant_id for h in vec)
        assert fts == []
        # and A still finds its own content
    mine = await search(sessionmaker, tenant_id, "hinta")
    assert [r.content for r in mine] == ["Salainen hinnasto 990 euroa"]


async def test_tenant_with_no_content_gets_nothing_from_a_populated_neighbour(
    sessionmaker: SM, tenant_id: uuid.UUID, other_tenant_id: uuid.UUID
) -> None:
    for i in range(5):
        await add(sessionmaker, tenant_id, f"u{i}", f"Hinnat versio {i}")
    assert await search(sessionmaker, other_tenant_id, "hinta") == []
    async with sessionmaker() as s:
        total = (await s.execute(select(func.count()).select_from(Source))).scalar_one()
        assert total >= 5  # the neighbour's rows exist in the same table


# ------------------------------------------------------------------ dual query (raw + expanded)


async def dual_search(
    sm: SM,
    tenant_id: uuid.UUID,
    query: str,
    raw_query: str | None,
    *,
    top_k: int = 5,
) -> tuple[list[RetrievedChunk], FakeEmbeddingProvider]:
    embedder, provider = embedder_for(sm)
    async with sm() as s:
        found = await retrieve(s, embedder, tenant_id, query, "fi", top_k, raw_query=raw_query)
    return found, provider


async def test_dual_query_embeds_both_texts_in_one_request(
    sessionmaker: SM, tenant_id: uuid.UUID
) -> None:
    await add(sessionmaker, tenant_id, "u1", "Hinnat alkavat 990 eurosta")
    _, provider = await dual_search(sessionmaker, tenant_id, "palvelu service", "hinta")
    assert provider.calls == [["palvelu service", "hinta"]]  # one request, expanded first
    async with sessionmaker() as s:
        rows = (
            await s.execute(
                select(func.count())
                .select_from(UsageEvent)
                .where(UsageEvent.tenant_id == tenant_id, UsageEvent.operation == "embed")
            )
        ).scalar_one()
    assert rows == 1


async def test_dual_query_relevance_is_the_max_over_both_vectors(
    sessionmaker: SM, tenant_id: uuid.UUID
) -> None:
    await add(sessionmaker, tenant_id, "u1", "Hinnat alkavat 990 eurosta")  # price axis
    # The expansion drifted to an unrelated axis; the visitor's own words are on topic.
    drifted, _ = await dual_search(sessionmaker, tenant_id, "satunnainen sekava laajennus", None)
    dual, _ = await dual_search(sessionmaker, tenant_id, "satunnainen sekava laajennus", "hinta")
    assert best_relevance(drifted) == pytest.approx(0.0)
    assert best_relevance(dual) == pytest.approx(1.0)
    assert dual[0].content.startswith("Hinnat")


async def test_dual_query_raw_leg_adds_candidates_the_expansion_misses(
    sessionmaker: SM, tenant_id: uuid.UUID
) -> None:
    await add(sessionmaker, tenant_id, "u1", "Hinnat alkavat 990 eurosta")  # price axis
    await add(sessionmaker, tenant_id, "u2", "Palvelumme kattavat kaiken")  # service axis
    found, _ = await dual_search(sessionmaker, tenant_id, "palvelu service", "hinta", top_k=2)
    assert {c.content.split()[0] for c in found} == {"Hinnat", "Palvelumme"}
    assert all(c.relevance == pytest.approx(1.0) for c in found)  # each is exact for one vector


@pytest.mark.parametrize("raw", [None, "", "   ", "palvelu service"])
async def test_single_vector_when_raw_is_absent_or_identical(
    sessionmaker: SM, tenant_id: uuid.UUID, raw: str | None
) -> None:
    await add(sessionmaker, tenant_id, "u1", "Palvelumme kattavat kaiken")
    found, provider = await dual_search(sessionmaker, tenant_id, "palvelu service", raw)
    assert provider.calls == [["palvelu service"]]
    assert best_relevance(found) == pytest.approx(1.0)


async def test_dual_query_never_returns_other_tenants_data(
    sessionmaker: SM, tenant_id: uuid.UUID, other_tenant_id: uuid.UUID
) -> None:
    await add(sessionmaker, other_tenant_id, "s1", "Salainen hinnasto")
    await add(sessionmaker, tenant_id, "u1", "Palvelumme kattavat kaiken")
    found, _ = await dual_search(sessionmaker, tenant_id, "palvelu service", "salainen hinta")
    assert all("Salainen" not in c.content for c in found)


async def test_extra_vector_leg_does_not_dilute_a_lexical_only_match(
    sessionmaker: SM, tenant_id: uuid.UUID
) -> None:
    """A rare term found only by full-text search (think "SEO") must stay on top of chunks that
    each only one vector leg finds: vector legs together weigh as much as the full-text leg."""
    for i in (1, 2):
        await add(sessionmaker, tenant_id, f"p{i}", f"Eurot alkaen {i}", vector=axis_vector(0))
        await add(
            sessionmaker, tenant_id, f"s{i}", f"Kattava kokonaisuus {i}", vector=axis_vector(1)
        )
    await add(
        sessionmaker, tenant_id, "r", "Erikoisosaamista: design ja seo", vector=axis_vector(500)
    )
    found, _ = await dual_search(sessionmaker, tenant_id, "hinta seo", "palvelu", top_k=3)
    assert found[0].content.startswith("Erikoisosaamista")
