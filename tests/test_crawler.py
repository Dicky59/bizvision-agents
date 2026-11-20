import httpx
import pytest

from core.ingest.crawler import (
    CrawledPage,
    CrawledPdf,
    CrawlFailure,
    CrawlItem,
    crawl,
    extract_page,
    normalize_url,
    parse_sitemap,
    preview,
    same_domain,
)
from core.ingest.errors import FetchError
from core.tenants.config import IngestConfig

SENTENCE = "Tarjoamme tekoälyratkaisuja pienille ja keskisuurille yrityksille Suomessa. "


def html_page(
    title: str,
    *,
    links: tuple[str, ...] = (),
    canonical: str | None = None,
    lang: str | None = "fi",
    extra: str = "",
) -> str:
    paras = "".join(f"<p>{title} kappale {i}. {SENTENCE * 3}</p>" for i in range(4))
    canon = f'<link rel="canonical" href="{canonical}">' if canonical else ""
    nav = "".join(f'<a href="{h}">linkki</a>' for h in links)
    lang_attr = f' lang="{lang}"' if lang else ""
    return (
        f"<html{lang_attr}><head><title>{title}</title>{canon}</head><body>"
        f"<nav>{nav}</nav><main><h1>{title}</h1>{paras}<h2>Hinta</h2>"
        f"<ul><li>Perus 990 €</li></ul>{extra}</main></body></html>"
    )


class Site:
    """Fake website served through httpx.MockTransport; records requested paths."""

    def __init__(self, routes: dict[str, httpx.Response | str], host: str = "example.com") -> None:
        self.routes = routes
        self.host = host
        self.requests: list[str] = []

    def handler(self, req: httpx.Request) -> httpx.Response:
        key = req.url.path + (f"?{req.url.query.decode()}" if req.url.query else "")
        if req.url.host == self.host:
            self.requests.append(key)
        route = self.routes.get(key)
        if route is None:
            return httpx.Response(404)
        if isinstance(route, str):
            return httpx.Response(200, text=route, headers={"content-type": "text/html"})
        return route


def xml(body: str) -> httpx.Response:
    return httpx.Response(200, text=body, headers={"content-type": "application/xml"})


def sitemap(*urls: str) -> httpx.Response:
    locs = "".join(f"<url><loc>{u}</loc></url>" for u in urls)
    return xml(
        f'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{locs}</urlset>"
    )


async def resolve_public(host: str) -> list[str]:
    return ["93.184.216.34"]


async def run(site: Site, **cfg: object) -> list[CrawlItem]:
    cfg.setdefault("start_urls", ["https://example.com"])
    config = IngestConfig.model_validate(cfg)
    async with httpx.AsyncClient(transport=httpx.MockTransport(site.handler)) as client:
        return [item async for item in crawl(config, client, resolver=resolve_public, delay=0)]


def pages(items: list[CrawlItem]) -> list[CrawledPage]:
    return [i for i in items if isinstance(i, CrawledPage)]


def urls(items: list[CrawlItem]) -> list[str]:
    return sorted(p.url for p in pages(items))


# ---------------------------------------------------------------- URL normalisation


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("HTTPS://Example.COM/Palvelut/", "https://example.com/Palvelut"),
        ("https://example.com", "https://example.com/"),
        ("https://example.com/", "https://example.com/"),
        ("https://example.com/a#section", "https://example.com/a"),
        ("https://example.com:443/a", "https://example.com/a"),
        ("http://example.com:80/a/", "http://example.com/a"),
        ("https://example.com:8443/a", "https://example.com:8443/a"),
        ("https://example.com/a/?x=1#f", "https://example.com/a?x=1"),
        ("  https://example.com/a  ", "https://example.com/a"),
    ],
)
def test_normalize_url(raw: str, expected: str) -> None:
    assert normalize_url(raw) == expected


def test_same_domain_treats_www_as_same_site() -> None:
    assert same_domain("https://www.example.com/a", "https://example.com/b")
    assert not same_domain("https://example.com", "https://evil.com")
    assert not same_domain("https://example.com", "https://sub.example.com")


# ---------------------------------------------------------------- extraction


def test_extraction_keeps_headings_lists_and_recall() -> None:
    page = extract_page(html_page("Palvelut", lang="fi-FI"), "https://example.com/palvelut")
    assert page is not None
    assert page.text.startswith("# Palvelut")
    assert "## Hinta" in page.text and "- Perus 990 €" in page.text
    assert page.title == "Palvelut"
    assert page.language == "fi"


def test_extraction_language_falls_back_to_detector_without_lang_attr() -> None:
    page = extract_page(html_page("Palvelut", lang=None), "https://example.com/")
    assert page is not None and page.language == "fi"


def test_extraction_canonical_and_links_are_absolute() -> None:
    html = html_page("X", canonical="/x/", links=("/a", "b.html", "https://other.com/c"))
    page = extract_page(html, "https://example.com/dir/x")
    assert page is not None
    assert page.canonical == "https://example.com/x/"
    assert page.links == [
        "https://example.com/a",
        "https://example.com/dir/b.html",
        "https://other.com/c",
    ]


def test_empty_page_has_no_text() -> None:
    page = extract_page("<html><body></body></html>", "https://example.com/")
    assert page is None or page.text == ""


# ---------------------------------------------------------------- sitemap parsing


def test_parse_sitemap_urlset_and_index() -> None:
    body = sitemap("https://example.com/a", "https://example.com/b").content
    assert parse_sitemap(body) == (["https://example.com/a", "https://example.com/b"], [])
    index = (
        b'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        b"<sitemap><loc>https://example.com/s1.xml</loc></sitemap></sitemapindex>"
    )
    assert parse_sitemap(index) == ([], ["https://example.com/s1.xml"])


def test_parse_sitemap_rejects_entities_and_garbage() -> None:
    bomb = (
        b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]>'
        b"<urlset><url><loc>&a;</loc></url></urlset>"
    )
    with pytest.raises(FetchError):
        parse_sitemap(bomb)
    with pytest.raises(FetchError):
        parse_sitemap(b"not xml at all <<<")


# ---------------------------------------------------------------- crawling


async def test_sitemap_mode_crawls_listed_urls_only() -> None:
    site = Site(
        {
            "/sitemap.xml": sitemap("https://example.com/", "https://example.com/palvelut"),
            "/": html_page("Etusivu", links=("/piilotettu",)),
            "/palvelut": html_page("Palvelut"),
            "/piilotettu": html_page("Piilotettu"),
        }
    )
    items = await run(site)
    assert urls(items) == ["https://example.com/", "https://example.com/palvelut"]
    assert "/piilotettu" not in site.requests  # links not followed when a sitemap exists


async def test_sitemap_index_is_followed() -> None:
    index = xml(
        '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        "<sitemap><loc>https://example.com/s1.xml</loc></sitemap></sitemapindex>"
    )
    site = Site(
        {
            "/sitemap.xml": index,
            "/s1.xml": sitemap("https://example.com/a"),
            "/": html_page("Etusivu"),
            "/a": html_page("A"),
        }
    )
    assert urls(await run(site)) == ["https://example.com/", "https://example.com/a"]


async def test_sitemap_listed_in_robots_txt() -> None:
    site = Site(
        {
            "/robots.txt": httpx.Response(
                200, text="User-agent: *\nAllow: /\nSitemap: https://example.com/maps/main.xml\n"
            ),
            "/maps/main.xml": sitemap("https://example.com/a"),
            "/": html_page("Etusivu"),
            "/a": html_page("A"),
        }
    )
    assert urls(await run(site)) == ["https://example.com/", "https://example.com/a"]


async def test_bfs_fallback_without_sitemap_same_domain_only() -> None:
    site = Site(
        {
            "/": html_page("Etusivu", links=("/a", "https://other.com/x", "mailto:x@y.fi")),
            "/a": html_page("A", links=("/b",)),
            "/b": html_page("B", links=("/", "/a#osa")),
        }
    )
    items = await run(site)
    assert urls(items) == [
        "https://example.com/",
        "https://example.com/a",
        "https://example.com/b",
    ]
    assert site.requests.count("/a") == 1  # fragments/duplicates are not refetched


async def test_use_sitemap_false_ignores_sitemap() -> None:
    site = Site(
        {
            "/sitemap.xml": sitemap("https://example.com/hidden"),
            "/": html_page("Etusivu"),
            "/hidden": html_page("Hidden"),
        }
    )
    items = await run(site, use_sitemap=False)
    assert urls(items) == ["https://example.com/"]
    assert "/sitemap.xml" not in site.requests


async def test_robots_disallow_is_respected() -> None:
    site = Site(
        {
            "/robots.txt": httpx.Response(200, text="User-agent: *\nDisallow: /private\n"),
            "/": html_page("Etusivu", links=("/private/a", "/public")),
            "/private/a": html_page("Secret"),
            "/public": html_page("Public"),
        }
    )
    items = await run(site)
    assert urls(items) == ["https://example.com/", "https://example.com/public"]
    assert "/private/a" not in site.requests


async def test_robots_5xx_means_disallow_all() -> None:
    site = Site({"/robots.txt": httpx.Response(503), "/": html_page("Etusivu")})
    assert await run(site) == []
    assert "/" not in site.requests


async def test_include_and_exclude_patterns() -> None:
    site = Site(
        {
            "/": html_page("Etusivu", links=("/blog/1", "/palvelut/a", "/api/x", "/palvelut?p=2")),
            "/blog/1": html_page("Blog"),
            "/palvelut/a": html_page("Palvelu"),
            "/api/x": html_page("API"),
            "/palvelut?p=2": html_page("Sivu 2"),
        }
    )
    items = await run(site, exclude_patterns=["/api/", "?"])
    assert urls(items) == [
        "https://example.com/",
        "https://example.com/blog/1",
        "https://example.com/palvelut/a",
    ]
    site.requests.clear()
    items = await run(site, include_patterns=["/palvelut"], exclude_patterns=["?"])
    # start URL does not match include patterns, so nothing is crawled
    assert items == []


async def test_max_pages_limits_fetches() -> None:
    routes: dict[str, httpx.Response | str] = {
        "/": html_page("Etusivu", links=tuple(f"/p{i}" for i in range(10)))
    }
    routes.update({f"/p{i}": html_page(f"P{i}") for i in range(10)})
    items = await run(Site(routes), max_pages=3)
    assert len(items) == 3


async def test_linked_pdf_is_yielded_as_raw_bytes_even_in_sitemap_mode() -> None:
    site = Site(
        {
            "/sitemap.xml": sitemap("https://example.com/"),
            "/": html_page("Etusivu", links=("/esite.pdf", "https://other.com/x.pdf")),
            "/esite.pdf": httpx.Response(
                200, content=b"%PDF-1.4 fake", headers={"content-type": "application/pdf"}
            ),
        }
    )
    items = await run(site)
    pdfs = [i for i in items if isinstance(i, CrawledPdf)]
    assert [(p.url, p.data) for p in pdfs] == [("https://example.com/esite.pdf", b"%PDF-1.4 fake")]


async def test_non_html_content_is_skipped() -> None:
    site = Site(
        {
            "/": html_page("Etusivu", links=("/kuva.png", "/data.json")),
            "/kuva.png": httpx.Response(
                200, content=b"\x89PNG", headers={"content-type": "image/png"}
            ),
            "/data.json": httpx.Response(200, json={"a": 1}),
        }
    )
    items = await run(site)
    assert urls(items) == ["https://example.com/"]
    assert not [i for i in items if isinstance(i, CrawlFailure)]


async def test_http_errors_become_failures_and_crawl_continues() -> None:
    site = Site(
        {
            "/": html_page("Etusivu", links=("/missing", "/ok")),
            "/ok": html_page("OK"),
        }
    )
    items = await run(site)
    assert urls(items) == ["https://example.com/", "https://example.com/ok"]
    failures = [i for i in items if isinstance(i, CrawlFailure)]
    assert [(f.url, f.reason) for f in failures] == [("https://example.com/missing", "HTTP 404")]


async def test_canonical_deduplicates_and_sets_identity() -> None:
    site = Site(
        {
            "/": html_page("Etusivu", links=("/palvelut", "/palvelut/index", "/palvelut?ref=x")),
            "/palvelut": html_page("Palvelut", canonical="https://example.com/palvelut/"),
            "/palvelut/index": html_page("Palvelut", canonical="https://example.com/palvelut"),
            "/palvelut?ref=x": html_page("Palvelut", canonical="https://EXAMPLE.com/palvelut#top"),
        }
    )
    items = await run(site)
    assert urls(items) == ["https://example.com/", "https://example.com/palvelut"]


async def test_canonical_to_other_domain_is_ignored() -> None:
    site = Site(
        {
            "/": html_page("Etusivu", links=("/a",)),
            "/a": html_page("A", canonical="https://evil.com/a"),
        }
    )
    assert urls(await run(site)) == ["https://example.com/", "https://example.com/a"]


async def test_redirect_within_site_uses_final_url_as_identity() -> None:
    site = Site(
        {
            "/": html_page("Etusivu", links=("/vanha",)),
            "/vanha": httpx.Response(301, headers={"location": "/uusi/"}),
            "/uusi/": html_page("Uusi"),  # the fake site matches paths exactly
        }
    )
    assert urls(await run(site)) == ["https://example.com/", "https://example.com/uusi"]


async def test_content_hash_is_stable_for_unchanged_page() -> None:
    routes: dict[str, httpx.Response | str] = {"/": html_page("Etusivu")}
    a = pages(await run(Site(routes)))[0]
    b = pages(await run(Site(routes)))[0]
    assert a.content_hash == b.content_hash and len(a.content_hash) == 64


# ---------------------------------------------------------------- SSRF behaviour


async def test_start_url_resolving_to_private_ip_is_not_fetched() -> None:
    site = Site({"/": html_page("Internal")})

    async def private(host: str) -> list[str]:
        return ["10.0.0.7"]

    config = IngestConfig.model_validate({"start_urls": ["https://example.com"]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(site.handler)) as client:
        items = [i async for i in crawl(config, client, resolver=private, delay=0)]
    assert site.requests == []
    assert [type(i) for i in items] == [CrawlFailure]
    assert "UnsafeUrl" in items[0].reason  # type: ignore[union-attr]


async def test_redirect_to_internal_host_is_blocked() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "example.com":
            return httpx.Response(302, headers={"location": "http://internal.test/admin"})
        raise AssertionError("internal host must never be requested")

    async def resolver(host: str) -> list[str]:
        return ["192.168.1.5"] if host == "internal.test" else ["93.184.216.34"]

    config = IngestConfig.model_validate(
        {"start_urls": ["https://example.com"], "use_sitemap": False}
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        items = [i async for i in crawl(config, client, resolver=resolver, delay=0)]
    assert len(items) == 1 and isinstance(items[0], CrawlFailure)


def test_preview_collapses_whitespace_and_truncates() -> None:
    assert preview("a\n\n  b\tc", 10) == "a b c"
    assert len(preview("x" * 1000)) == 300


async def test_crawler_is_stateless_and_never_sends_cookies() -> None:
    """Sites that remember a locale cookie must not change what later pages return."""
    seen_cookies: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if cookie := req.headers.get("cookie"):
            seen_cookies.append(cookie)
        headers = {"content-type": "text/html", "set-cookie": "locale=fi; Path=/"}
        if req.url.path == "/":
            return httpx.Response(
                200, text=html_page("Etusivu", links=("/about",)), headers=headers
            )
        if req.url.path == "/about":
            if "locale=fi" in req.headers.get("cookie", ""):  # what a sticky-locale site would do
                return httpx.Response(200, text=html_page("Meistä", lang="fi"), headers=headers)
            return httpx.Response(200, text=html_page("About", lang="en"), headers=headers)
        return httpx.Response(404)

    config = IngestConfig.model_validate(
        {"start_urls": ["https://example.com"], "use_sitemap": False}
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        items = [i async for i in crawl(config, client, resolver=resolve_public, delay=0)]
    about = next(p for p in pages(items) if p.url.endswith("/about"))
    assert about.title == "About" and about.language == "en"
    assert seen_cookies == []
