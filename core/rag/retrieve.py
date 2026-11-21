"""Hybrid retrieval: vector search + full-text search fused with Reciprocal Rank Fusion.

Every query filters by tenant_id (on chunks *and* sources) and by `sources.status = 'active'`.
Requires pgvector >= 0.8 (iterative HNSW scans; see `_VECTOR_PRELUDE`).
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import Select, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from core.db.models import Chunk, Source
from core.db.textsearch import ts_config_expr
from core.embeddings.client import EmbeddingClient
from core.rag.query import build_or_query
from core.rag.rrf import RRF_K, fuse

CANDIDATES = 20  # per search leg
FAQ_BOOST = 1.15
LANGUAGE_BOOST = 1.2  # soft preference: translated duplicates must not crowd out top_k

# A tenant filter applied after an HNSW scan can leave fewer than LIMIT rows on a shared table;
# iterative scans keep scanning until enough rows pass the filter.
_VECTOR_PRELUDE = text("SET LOCAL hnsw.iterative_scan = 'strict_order'")


@dataclass(frozen=True)
class RetrievedChunk:
    chunk_id: uuid.UUID
    source_id: uuid.UUID
    source_type: str  # web | file | faq
    title: str | None
    url: str | None  # page URL for web sources; None for files and FAQ entries
    heading_path: str | None
    content: str
    language: str | None
    is_faq: bool
    relevance: float  # cosine similarity to the query, clamped to 0..1
    score: float  # fused RRF score including boosts (ordering only; not comparable across queries)


def best_relevance(chunks: list[RetrievedChunk]) -> float:
    """Confidence signal for the assistant: the best similarity among the results (0 if none)."""
    return max((c.relevance for c in chunks), default=0.0)


@dataclass(frozen=True)
class _Hit:
    chunk: Chunk
    source: Source
    similarity: float


def _base(tenant_id: uuid.UUID, qvecs: list[list[float]]) -> Select:
    """Similarity is the best cosine similarity across all query vectors (raw + expanded)."""
    sims = [1 - Chunk.embedding.cosine_distance(v) for v in qvecs]
    similarity = (sims[0] if len(sims) == 1 else func.greatest(*sims)).label("similarity")
    return (
        select(Chunk, Source, similarity)
        .join(Source, Source.id == Chunk.source_id)
        .where(
            Chunk.tenant_id == tenant_id,
            Source.tenant_id == tenant_id,
            Source.status == "active",
            Chunk.embedding.is_not(None),
        )
    )


async def vector_search(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    qvec: list[float],
    limit: int = CANDIDATES,
    *,
    similarity_vecs: list[list[float]] | None = None,
) -> list[_Hit]:
    """Nearest chunks to `qvec`; each hit's similarity is the max over `similarity_vecs`."""
    await session.execute(_VECTOR_PRELUDE)
    stmt = _base(tenant_id, similarity_vecs or [qvec]).order_by(
        Chunk.embedding.cosine_distance(qvec), Chunk.id
    )
    rows = (await session.execute(stmt.limit(limit))).all()
    return [_Hit(c, s, float(sim)) for c, s, sim in rows]


async def fulltext_search(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    query: str,
    language: str | None,
    qvec: list[float],
    limit: int = CANDIDATES,
    *,
    similarity_vecs: list[list[float]] | None = None,
) -> list[_Hit]:
    or_query = build_or_query(query)
    if or_query is None:
        return []
    tsq = func.to_tsquery(ts_config_expr(language), or_query)
    rank = func.ts_rank_cd(Chunk.tsv, tsq)
    stmt = (
        _base(tenant_id, similarity_vecs or [qvec])
        .where(Chunk.tsv.op("@@")(tsq))
        .order_by(rank.desc(), Chunk.id)
    )
    rows = (await session.execute(stmt.limit(limit))).all()
    return [_Hit(c, s, float(sim)) for c, s, sim in rows]


async def retrieve(
    session: AsyncSession,
    embedder: EmbeddingClient,
    tenant_id: uuid.UUID,
    query: str,
    language: str | None,
    top_k: int,
    *,
    conversation_id: uuid.UUID | None = None,
    raw_query: str | None = None,
) -> list[RetrievedChunk]:
    """Top `top_k` chunks for `query`, best first.

    `language` is the query language ('fi'/'en'): it picks the full-text config and gives chunks
    in the same language a soft boost (no hard filter, so other-language content is still found).

    `query` is the (possibly LLM-expanded) search query; `raw_query` is the visitor's own message.
    When both are given and differ, they are embedded in ONE request, each gets its own vector
    leg, the full-text leg runs on `query`, and all legs are fused with RRF. A chunk's relevance
    is its best similarity to either vector, so the score no longer depends on how the expansion
    happened to be worded.
    """
    if not query.strip() or top_k < 1:
        return []
    texts = [query]
    if raw_query and raw_query.strip() and raw_query.strip() != query.strip():
        texts.append(raw_query)
    qvecs = await embedder.embed(tenant_id, texts, conversation_id=conversation_id)

    vector_legs = [await vector_search(session, tenant_id, v, similarity_vecs=qvecs) for v in qvecs]
    text_hits = await fulltext_search(
        session, tenant_id, query, language, qvecs[0], similarity_vecs=qvecs
    )

    hits: dict[uuid.UUID, _Hit] = {
        h.chunk.id: h for h in (*text_hits, *(h for leg in vector_legs for h in leg))
    }
    scores = fuse(
        [[h.chunk.id for h in leg] for leg in vector_legs] + [[h.chunk.id for h in text_hits]],
        k=RRF_K,
    )
    for chunk_id, hit in hits.items():
        if hit.source.type == "faq":
            scores[chunk_id] *= FAQ_BOOST
        if language is not None and hit.chunk.language == language:
            scores[chunk_id] *= LANGUAGE_BOOST

    ranked = sorted(scores, key=lambda cid: (-scores[cid], str(cid)))[:top_k]
    return [
        RetrievedChunk(
            chunk_id=cid,
            source_id=hits[cid].source.id,
            source_type=hits[cid].source.type,
            title=hits[cid].source.title,
            url=hits[cid].source.uri if hits[cid].source.type == "web" else None,
            heading_path=hits[cid].chunk.heading_path,
            content=hits[cid].chunk.content,
            language=hits[cid].chunk.language,
            is_faq=hits[cid].source.type == "faq",
            relevance=min(1.0, max(0.0, hits[cid].similarity)),
            score=scores[cid],
        )
        for cid in ranked
    ]
