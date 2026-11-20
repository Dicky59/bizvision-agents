import httpx
import pytest

from core.ingest.errors import ResponseTooLarge, TooManyRedirects, UnsafeUrl
from core.ingest.safety import is_public_ip, safe_get, validate_url


def resolver_for(mapping: dict[str, list[str]]):
    async def resolve(host: str) -> list[str]:
        return mapping.get(host, ["93.184.216.34"])

    return resolve


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",
        "10.1.2.3",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",  # cloud metadata
        "100.64.0.1",  # CGNAT
        "0.0.0.0",
        "224.0.0.1",
        "240.0.0.1",
        "::1",
        "fe80::1",
        "fc00::1",
        "ff02::1",
        "::",
        "::ffff:127.0.0.1",  # IPv4-mapped loopback
        "::ffff:10.0.0.1",
        "not-an-ip",
    ],
)
def test_non_public_addresses_rejected(ip: str) -> None:
    assert not is_public_ip(ip)


@pytest.mark.parametrize("ip", ["93.184.216.34", "8.8.8.8", "2606:2800:220:1:248:1893:25c8:1946"])
def test_public_addresses_allowed(ip: str) -> None:
    assert is_public_ip(ip)


@pytest.mark.parametrize(
    "url", ["file:///etc/passwd", "ftp://example.com/x", "gopher://example.com", "//example.com"]
)
async def test_scheme_must_be_http(url: str) -> None:
    with pytest.raises(UnsafeUrl):
        await validate_url(url, resolver_for({}))


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://[::1]/",
        "http://169.254.169.254/latest/meta-data",
        "http://0.0.0.0",
    ],
)
async def test_ip_literals_rejected(url: str) -> None:
    with pytest.raises(UnsafeUrl):
        await validate_url(url, resolver_for({}))


async def test_hostname_resolving_to_private_rejected() -> None:
    with pytest.raises(UnsafeUrl):
        await validate_url("https://evil.test/", resolver_for({"evil.test": ["10.0.0.5"]}))


async def test_any_private_address_among_several_rejects() -> None:
    r = resolver_for({"mixed.test": ["93.184.216.34", "127.0.0.1"]})
    with pytest.raises(UnsafeUrl):
        await validate_url("https://mixed.test/", r)


async def test_public_host_ok() -> None:
    await validate_url("https://example.com/", resolver_for({}))


def client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_safe_get_returns_body() -> None:
    async with client(lambda r: httpx.Response(200, text="hello")) as c:
        resp, body = await safe_get(c, "https://example.com/", resolver=resolver_for({}))
    assert resp.status_code == 200 and body == b"hello"


async def test_redirect_to_private_host_is_blocked() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "example.com":
            return httpx.Response(302, headers={"location": "http://internal.test/admin"})
        return httpx.Response(200, text="secret")

    r = resolver_for({"internal.test": ["192.168.0.10"]})
    async with client(handler) as c:
        with pytest.raises(UnsafeUrl):
            await safe_get(c, "https://example.com/", resolver=r)


async def test_relative_redirect_followed() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/a":
            return httpx.Response(301, headers={"location": "/b"})
        return httpx.Response(200, text="ok")

    async with client(handler) as c:
        resp, body = await safe_get(c, "https://example.com/a", resolver=resolver_for({}))
    assert str(resp.url) == "https://example.com/b" and body == b"ok"


async def test_redirect_loop_stops_after_five() -> None:
    calls = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(302, headers={"location": "/loop"})

    async with client(handler) as c:
        with pytest.raises(TooManyRedirects):
            await safe_get(c, "https://example.com/loop", resolver=resolver_for({}))
    assert calls == 6  # initial request + 5 redirects


async def test_five_redirects_are_allowed() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        n = int(req.url.path.strip("/") or 0)
        if n < 5:
            return httpx.Response(302, headers={"location": f"/{n + 1}"})
        return httpx.Response(200, text="end")

    async with client(handler) as c:
        _, body = await safe_get(c, "https://example.com/0", resolver=resolver_for({}))
    assert body == b"end"


async def test_streamed_body_over_limit_aborts() -> None:
    big = httpx.Response(200, content=b"x" * 2000)  # no content-length check needed: stream cap
    async with client(lambda r: big) as c:
        with pytest.raises(ResponseTooLarge):
            await safe_get(c, "https://example.com/", resolver=resolver_for({}), max_bytes=1000)


async def test_declared_content_length_over_limit_rejected() -> None:
    resp = httpx.Response(200, headers={"content-length": "999999"}, content=b"x")
    async with client(lambda r: resp) as c:
        with pytest.raises(ResponseTooLarge):
            await safe_get(c, "https://example.com/", resolver=resolver_for({}), max_bytes=1000)
