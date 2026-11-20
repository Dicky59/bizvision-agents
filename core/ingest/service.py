"""Ingestion service: documents -> chunks -> embeddings -> database.

Design notes (see ADR-004):
- The embedding call happens *before* the write transaction, so no DB transaction is held open
  across a network call, and a failed embedding leaves the previous chunks untouched.
- One page = one transaction (source upsert + chunk replacement), so a failure only affects
  that page.
- Unchanged pages (same content_hash, still active) are skipped without any embedding call.
- Removal is conservative: nothing is marked removed when the crawl was truncated by
  max_pages or produced no pages at all, so an outage can never wipe a tenant's index.
"""

import asyncio
import hashlib
import logging
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import PurePosixPath
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.embeddings.client import EmbeddingClient
from core.ingest import repository as repo
from core.ingest.chunking import ChunkDraft, chunk_text, embedding_text, estimate_tokens
from core.ingest.crawler import (
    DEFAULT_DELAY,
    CrawledPage,
    CrawlFailure,
    CrawlReport,
    crawl,
)
from core.ingest.errors import IngestError
from core.ingest.files import parse_file
from core.ingest.language import detect_language
from core.ingest.safety import Resolver, system_resolver
from core.tenants.config import IngestConfig

log = logging.getLogger(__name__)

Outcome = Literal["new", "updated", "unchanged"]
Progress = Callable[[str, str], None]  # (outcome or "error"/"removed", url or name)

MAX_ERROR_SAMPLES = 20
MAX_FAQ_CHARS = 20_000
_GONE_REASONS = ("HTTP 404", "HTTP 410")  # page is gone; other failures are treated as transient


@dataclass
class IngestStats:
    new: int = 0
    updated: int = 0
    unchanged: int = 0
    removed: int = 0
    errors: int = 0
    chunks: int = 0
    removal_skipped: str | None = None
    error_samples: list[str] = field(default_factory=list)

    @property
    def succeeded(self) -> int:
        return self.new + self.updated + self.unchanged

    def add_error(self, where: str, reason: str) -> None:
        self.errors += 1
        if len(self.error_samples) < MAX_ERROR_SAMPLES:
            self.error_samples.append(f"{where}: {reason}")

    def record(self, outcome: Outcome, chunks: int) -> None:
        setattr(self, outcome, getattr(self, outcome) + 1)
        self.chunks += chunks

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def ingest_document(
    sessionmaker: async_sessionmaker[AsyncSession],
    embedder: EmbeddingClient,
    tenant_id: uuid.UUID,
    *,
    type_: str,
    key: str,
    title: str | None,
    text: str,
    language: str | None,
    hash_: str | None = None,
    single_chunk: bool = False,
) -> tuple[Outcome, int]:
    """Index one document. Returns (outcome, chunks written)."""
    hash_ = hash_ or content_hash(text)
    async with sessionmaker() as session:
        existing = await repo.get_source(session, tenant_id, type_, key)
        if existing is not None and existing.status == "active" and existing.content_hash == hash_:
            await repo.touch_source(session, tenant_id, existing.id)
            await session.commit()
            return "unchanged", 0
        was_active = existing is not None and existing.status == "active"

    if single_chunk:  # FAQ entries: one chunk, and the question is already part of the content
        drafts = [ChunkDraft(0, text, None, estimate_tokens(text))] if text.strip() else []
        embed_title = None
    else:
        drafts = chunk_text(text)
        embed_title = title
    if not drafts:
        raise IngestError("no content to index")

    texts = [embedding_text(embed_title, d.heading_path, d.content) for d in drafts]
    vectors = await embedder.embed(tenant_id, texts)
    if len(vectors) != len(drafts):
        raise IngestError(f"embedding returned {len(vectors)} vectors for {len(drafts)} chunks")

    async with sessionmaker() as session:
        source = await repo.upsert_source(
            session,
            tenant_id,
            type_=type_,
            key=key,
            title=title,
            language=language,
            content_hash=hash_,
        )
        written = await repo.replace_chunks(
            session,
            tenant_id,
            source.id,
            title=title,
            language=language,
            drafts=drafts,
            vectors=vectors,
        )
        await session.commit()
    return ("updated" if was_active else "new"), written


async def _start_job(
    sessionmaker: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, kind: str
) -> uuid.UUID:
    async with sessionmaker() as session:
        job = await repo.create_job(session, tenant_id, kind)
        await session.commit()
        return job.id


async def _finish_job(
    sessionmaker: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    job_id: uuid.UUID,
    stats: IngestStats,
    *,
    status: str,
    error: str | None = None,
) -> None:
    async with sessionmaker() as session:
        await repo.finish_job(
            session, tenant_id, job_id, status=status, stats=stats.to_dict(), error=error
        )
        await session.commit()


def _pdf_name(url: str) -> str:
    name = PurePosixPath(urlsplit(url).path).name
    return name if name.lower().endswith(".pdf") else "linked.pdf"


async def crawl_tenant(
    sessionmaker: async_sessionmaker[AsyncSession],
    embedder: EmbeddingClient,
    tenant_id: uuid.UUID,
    config: IngestConfig,
    client: httpx.AsyncClient,
    *,
    resolver: Resolver = system_resolver,
    delay: float = DEFAULT_DELAY,
    progress: Progress | None = None,
) -> IngestStats:
    """Crawl the tenant's site and index it; re-runs skip unchanged pages."""
    stats = IngestStats()
    job_id = await _start_job(sessionmaker, tenant_id, "crawl")
    report = CrawlReport()
    seen: set[str] = set()  # source identities that still exist on the site
    notify = progress or (lambda _o, _u: None)

    try:
        async for item in crawl(config, client, resolver=resolver, delay=delay, report=report):
            if isinstance(item, CrawlFailure):
                stats.add_error(item.url, item.reason)
                if not item.reason.startswith(_GONE_REASONS):
                    seen.add(item.url)  # transient failure: keep what we have
                notify("error", item.url)
                continue
            try:
                if isinstance(item, CrawledPage):
                    key, title, text, language = item.url, item.title, item.text, item.language
                    hash_ = item.content_hash
                else:  # CrawledPdf
                    doc = await asyncio.to_thread(parse_file, _pdf_name(item.url), item.data)
                    key, title, text, language = item.url, doc.title, doc.text, doc.language
                    hash_ = content_hash(text)
                seen.add(key)
                outcome, written = await ingest_document(
                    sessionmaker,
                    embedder,
                    tenant_id,
                    type_="web",
                    key=key,
                    title=title or key,
                    text=text,
                    language=language,
                    hash_=hash_,
                )
                stats.record(outcome, written)
                notify(outcome, key)
            except Exception as e:  # one bad page must not stop the crawl
                url = item.url
                seen.add(url)  # keep any previously indexed version
                stats.add_error(url, f"{type(e).__name__}: {e}")
                log.warning("ingest failed tenant=%s error=%s", tenant_id, type(e).__name__)
                notify("error", url)

        await _remove_missing(sessionmaker, tenant_id, seen, report, stats, notify)
    except BaseException as e:
        await _finish_job(sessionmaker, tenant_id, job_id, stats, status="failed", error=str(e))
        raise

    if stats.succeeded == 0:
        await _finish_job(
            sessionmaker, tenant_id, job_id, stats, status="failed", error="no pages ingested"
        )
    else:
        await _finish_job(sessionmaker, tenant_id, job_id, stats, status="done")
    log.info(
        "crawl finished tenant=%s new=%d updated=%d unchanged=%d removed=%d errors=%d",
        tenant_id,
        stats.new,
        stats.updated,
        stats.unchanged,
        stats.removed,
        stats.errors,
    )
    return stats


async def _remove_missing(
    sessionmaker: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    seen: set[str],
    report: CrawlReport,
    stats: IngestStats,
    notify: Progress,
) -> None:
    if report.truncated:
        stats.removal_skipped = "crawl stopped at max_pages; raise it to enable removal"
        return
    if stats.succeeded == 0:
        stats.removal_skipped = "no pages were crawled successfully"
        return
    async with sessionmaker() as session:
        active = await repo.list_sources(session, tenant_id, type_="web", status="active")
        gone = [s for s in active if s.uri not in seen]
        await repo.mark_removed(session, tenant_id, [s.id for s in gone])
        await session.commit()
    stats.removed = len(gone)
    for s in gone:
        notify("removed", s.uri or "")


async def ingest_file(
    sessionmaker: async_sessionmaker[AsyncSession],
    embedder: EmbeddingClient,
    tenant_id: uuid.UUID,
    filename: str,
    data: bytes,
) -> IngestStats:
    """Parse and index an uploaded file (identity = filename). Raises IngestError subclasses."""
    stats = IngestStats()
    job_id = await _start_job(sessionmaker, tenant_id, "file")
    try:
        doc = await asyncio.to_thread(parse_file, filename, data)
        outcome, written = await ingest_document(
            sessionmaker,
            embedder,
            tenant_id,
            type_="file",
            key=filename,
            title=doc.title,
            text=doc.text,
            language=doc.language,
        )
    except BaseException as e:
        stats.add_error(filename, f"{type(e).__name__}: {e}")
        await _finish_job(sessionmaker, tenant_id, job_id, stats, status="failed", error=str(e))
        raise
    stats.record(outcome, written)
    await _finish_job(sessionmaker, tenant_id, job_id, stats, status="done")
    return stats


@dataclass(frozen=True)
class FaqEntry:
    question: str
    answer: str


def faq_key(question: str) -> str:
    normalised = " ".join(question.split()).casefold()
    return "faq:" + hashlib.sha256(normalised.encode("utf-8")).hexdigest()[:16]


async def replace_faq(
    sessionmaker: async_sessionmaker[AsyncSession],
    embedder: EmbeddingClient,
    tenant_id: uuid.UUID,
    entries: list[FaqEntry],
) -> IngestStats:
    """Make the tenant's FAQ exactly `entries`: new/changed are embedded, absent ones deleted."""
    cleaned: dict[str, FaqEntry] = {}
    for e in entries:
        q, a = e.question.strip(), e.answer.strip()
        if not q or not a:
            raise IngestError("FAQ entries need a non-empty question and answer")
        if len(q) + len(a) > MAX_FAQ_CHARS:
            raise IngestError(f"FAQ entry too long ({len(q) + len(a)} chars): {q[:60]!r}")
        key = faq_key(q)
        if key in cleaned:
            raise IngestError(f"duplicate FAQ question: {q[:60]!r}")
        cleaned[key] = FaqEntry(q, a)

    stats = IngestStats()
    for key, entry in cleaned.items():
        text = f"{entry.question}\n\n{entry.answer}"
        try:
            outcome, written = await ingest_document(
                sessionmaker,
                embedder,
                tenant_id,
                type_="faq",
                key=key,
                title=entry.question,
                text=text,
                language=detect_language(text),
                single_chunk=True,
            )
        except Exception as ex:
            stats.add_error(key, f"{type(ex).__name__}: {ex}")
            continue
        stats.record(outcome, written)

    if stats.errors:  # don't delete anything while some entries failed to index
        stats.removal_skipped = "some entries failed; nothing was deleted"
        return stats
    async with sessionmaker() as session:
        existing = await repo.list_sources(session, tenant_id, type_="faq")
        stale = [s.id for s in existing if s.uri not in cleaned]
        stats.removed = await repo.delete_sources(session, tenant_id, stale)
        await session.commit()
    return stats
