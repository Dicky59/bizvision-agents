"""Polite, SSRF-safe website crawler: robots.txt, sitemap-first with BFS fallback, trafilatura.

All network access goes through `core.ingest.safety.safe_get`. The httpx client is injected
(tests pass a MockTransport) and must not follow redirects itself.
"""

import asyncio
import hashlib
import logging
import re
import xml.etree.ElementTree as ET
from collections.abc import AsyncIterator
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx
import trafilatura
from lxml import html as lxml_html

from core.ingest.errors import FetchError, IngestError
from core.ingest.language import detect_language
from core.ingest.safety import Resolver, safe_get, system_resolver
from core.tenants.config import IngestConfig

log = logging.getLogger(__name__)

USER_AGENT = "BizVisionBot/1.0 (+https://bizvision.fi/bot)"
REQUEST_TIMEOUT = 15.0
CONCURRENCY = 4
DEFAULT_DELAY = 0.5
MAX_SITEMAPS = 20
MAX_SITEMAP_DEPTH = 3
_DEFAULT_PORTS = {"http": 80, "https": 443}


@dataclass(frozen=True)
class CrawledPage:
    url: str  # normalised identity (canonical when same-domain)
    fetched_url: str
    title: str
    text: str
    language: str | None
    content_hash: str


@dataclass(frozen=True)
class CrawledPdf:
    url: str
    data: bytes


@dataclass(frozen=True)
class CrawlFailure:
    url: str
    reason: str


CrawlItem = CrawledPage | CrawledPdf | CrawlFailure


def make_client() -> httpx.AsyncClient:
    """Client for the crawler: no automatic redirects (safe_get validates each hop)."""
    return httpx.AsyncClient(
        timeout=REQUEST_TIMEOUT, follow_redirects=False, headers={"User-Agent": USER_AGENT}
    )


# ---------------------------------------------------------------- URL helpers


def normalize_url(url: str) -> str:
    """Lowercase scheme/host, drop default port and fragment, strip trailing slash (not on root)."""
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    port = parts.port
    netloc = host if port is None or _DEFAULT_PORTS.get(scheme) == port else f"{host}:{port}"
    path = parts.path or "/"
    if len(path) > 1:
        path = path.rstrip("/") or "/"
    return urlunsplit((scheme, netloc, path, parts.query, ""))


def _site(host: str | None) -> str:
    h = (host or "").lower()
    return h[4:] if h.startswith("www.") else h


def same_domain(a: str, b: str) -> bool:
    """Same site, treating www.example.com and example.com as one."""
    return _site(urlsplit(a).hostname) == _site(urlsplit(b).hostname)


def _is_pdf_url(url: str) -> bool:
    return urlsplit(url).path.lower().endswith(".pdf")


def _allowed_by_patterns(url: str, include: list[str], exclude: list[str]) -> bool:
    if any(p in url for p in exclude):
        return False
    return not include or any(p in url for p in include)


# ---------------------------------------------------------------- parsing helpers


def parse_sitemap(xml_bytes: bytes) -> tuple[list[str], list[str]]:
    """Return (page_urls, child_sitemap_urls). Rejects DTDs/entities (XML bombs)."""
    head = xml_bytes[:4096].lower()
    if b"<!doctype" in head or b"<!entity" in xml_bytes[:65536].lower():
        raise FetchError("sitemap contains a DTD/entity declaration")
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        raise FetchError(f"invalid sitemap XML: {e}") from e
    tag = root.tag.rsplit("}", 1)[-1]
    locs = [
        (el.text or "").strip()
        for el in root.iter()
        if el.tag.rsplit("}", 1)[-1] == "loc" and (el.text or "").strip()
    ]
    return ([], locs) if tag == "sitemapindex" else (locs, [])


@dataclass(frozen=True)
class PageData:
    title: str
    text: str
    language: str | None
    canonical: str | None
    links: list[str]


def extract_page(html_text: str, page_url: str) -> PageData | None:
    """Pure/sync extraction (run in a thread): main content as markdown + links/canonical."""
    text = trafilatura.extract(
        html_text,
        url=page_url,
        favor_recall=True,
        include_formatting=True,
        include_tables=True,
        include_links=False,
        output_format="markdown",
    )
    try:
        tree = lxml_html.fromstring(html_text)
    except (ValueError, lxml_html.etree.ParserError):
        return None
    lang_attr = (tree.get("lang") or tree.get("xml:lang") or "").strip() or None
    canonical = None
    for link in tree.xpath("//link[@href]"):
        if "canonical" in (link.get("rel") or "").lower().split():
            canonical = urljoin(page_url, link.get("href"))
            break
    links = [urljoin(page_url, h) for h in tree.xpath("//a/@href")]
    title = ""
    meta = trafilatura.extract_metadata(html_text)
    if meta is not None and meta.title:
        title = meta.title.strip()
    if not title:
        t = tree.xpath("//title/text()")
        title = t[0].strip() if t else ""
    cleaned = (text or "").strip()
    if not cleaned:
        return PageData(title, "", None, canonical, links)
    return PageData(title, cleaned, detect_language(cleaned, lang_attr), canonical, links)


# ---------------------------------------------------------------- crawler


class _Robots:
    def __init__(self) -> None:
        self._parser: RobotFileParser | None = None
        self.sitemaps: list[str] = []
        self._disallow_all = False

    def load(self, status: int, body: bytes) -> None:
        if status >= 500:  # RFC 9309: unreachable robots.txt -> assume disallow
            self._disallow_all = True
            return
        if status >= 400:
            return  # no robots.txt -> everything allowed
        rp = RobotFileParser()
        rp.parse(body.decode("utf-8", errors="replace").splitlines())
        self._parser = rp
        self.sitemaps = list(rp.site_maps() or [])

    def allowed(self, url: str) -> bool:
        if self._disallow_all:
            return False
        return self._parser is None or self._parser.can_fetch(USER_AGENT, url)


async def crawl(
    config: IngestConfig,
    client: httpx.AsyncClient,
    *,
    resolver: Resolver = system_resolver,
    delay: float = DEFAULT_DELAY,
    concurrency: int = CONCURRENCY,
) -> AsyncIterator[CrawlItem]:
    """Crawl per tenant ingest config, yielding pages, PDFs (raw bytes) and failures."""
    start_urls = [normalize_url(u) for u in config.start_urls]
    if not start_urls:
        return
    headers = {"User-Agent": USER_AGENT}
    robots: dict[str, _Robots] = {}

    async def robots_for(url: str) -> _Robots:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in robots:
            r = _Robots()
            robots[origin] = r
            try:
                resp, body = await safe_get(
                    client, f"{origin}/robots.txt", resolver=resolver, headers=headers
                )
                r.load(resp.status_code, body)
            except IngestError as e:
                log.info("robots.txt unavailable for %s: %s", origin, e)
        return robots[origin]

    async def allowed(url: str) -> bool:
        return (await robots_for(url)).allowed(url)

    def wanted(url: str) -> bool:
        if urlsplit(url).scheme not in ("http", "https"):
            return False
        if not any(same_domain(url, s) for s in start_urls):
            return False
        return _allowed_by_patterns(url, config.include_patterns, config.exclude_patterns)

    async def fetch_xml(url: str) -> bytes | None:
        try:
            resp, body = await safe_get(client, url, resolver=resolver, headers=headers)
        except IngestError as e:
            log.info("sitemap fetch failed for %s: %s", url, e)
            return None
        return body if resp.status_code == 200 else None

    async def discover_sitemap_urls() -> list[str]:
        candidates: list[str] = []
        for s in start_urls:
            parts = urlsplit(s)
            origin = f"{parts.scheme}://{parts.netloc}"
            candidates.extend((await robots_for(s)).sitemaps)
            candidates.append(f"{origin}/sitemap.xml")
        pages: list[str] = []
        seen_maps: set[str] = set()
        frontier = [(c, 0) for c in dict.fromkeys(candidates)]
        while frontier and len(seen_maps) < MAX_SITEMAPS:
            sm_url, depth = frontier.pop(0)
            if sm_url in seen_maps:
                continue
            seen_maps.add(sm_url)
            body = await fetch_xml(sm_url)
            if body is None:
                continue
            try:
                urls, children = parse_sitemap(body)
            except FetchError as e:
                log.info("ignoring sitemap %s: %s", sm_url, e)
                continue
            pages.extend(urls)
            if depth + 1 < MAX_SITEMAP_DEPTH:
                frontier.extend((c, depth + 1) for c in children)
        return pages

    queue: list[str] = []
    queued: set[str] = set()

    def enqueue(url: str) -> None:
        n = normalize_url(url)
        if n not in queued and wanted(n):
            queued.add(n)
            queue.append(n)

    # start URLs are always crawled (subject to robots + patterns), then sitemap URLs
    for s in start_urls:
        enqueue(s)
    sitemap_found = False
    if config.use_sitemap:
        sitemap_urls = await discover_sitemap_urls()
        sitemap_found = bool(sitemap_urls)
        for u in sitemap_urls:
            enqueue(u)

    emitted: set[str] = set()  # identities already yielded (canonical dedup)
    attempted = 0
    sem = asyncio.Semaphore(concurrency)

    async def process(url: str) -> list[CrawlItem]:
        async with sem:
            if not await allowed(url):
                return []
            try:
                resp, body = await safe_get(client, url, resolver=resolver, headers=headers)
            except IngestError as e:
                return [CrawlFailure(url, f"{type(e).__name__}: {e}")]
            except httpx.HTTPError as e:
                return [CrawlFailure(url, f"{type(e).__name__}: {e}")]
        if resp.status_code != 200:
            return [CrawlFailure(url, f"HTTP {resp.status_code}")]
        ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
        final_url = normalize_url(str(resp.url))
        if ctype == "application/pdf" or (not ctype and _is_pdf_url(final_url)):
            return [CrawledPdf(url=url, data=body)]
        if ctype not in ("text/html", "application/xhtml+xml"):
            return []  # non-HTML, non-PDF: skipped silently
        html_text = body.decode(resp.encoding or "utf-8", errors="replace")
        page = await asyncio.to_thread(extract_page, html_text, str(resp.url))
        if page is None or not page.text:
            return [CrawlFailure(url, "no extractable content")]
        identity = final_url if same_domain(final_url, url) else url
        if page.canonical:
            canon = normalize_url(page.canonical)
            if urlsplit(canon).scheme in ("http", "https") and same_domain(canon, url):
                identity = canon
        for link in page.links:
            n = normalize_url(link)
            if _is_pdf_url(n):
                enqueue(n)  # linked PDFs are always queued (same domain, patterns apply)
            elif not sitemap_found:
                enqueue(n)
        return [
            CrawledPage(
                url=identity,
                fetched_url=final_url,
                title=page.title,
                text=page.text,
                language=page.language,
                content_hash=hashlib.sha256(page.text.encode("utf-8")).hexdigest(),
            )
        ]

    while queue and attempted < config.max_pages:
        batch_size = min(concurrency, config.max_pages - attempted, len(queue))
        batch, queue[:] = queue[:batch_size], queue[batch_size:]
        attempted += len(batch)
        for items in await asyncio.gather(*(process(u) for u in batch)):
            for item in items:
                if isinstance(item, CrawledPage):
                    if item.url in emitted:
                        continue  # same canonical page reached under another URL
                    emitted.add(item.url)
                    queued.add(item.url)  # never fetch the canonical form again
                yield item
        if delay > 0 and queue:
            await asyncio.sleep(delay)


_WS = re.compile(r"\s+")


def preview(text: str, n: int = 300) -> str:
    """Single-line preview for `bv ingest --dry-run`."""
    return _WS.sub(" ", text).strip()[:n]
