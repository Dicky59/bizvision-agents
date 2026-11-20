"""SSRF guard for everything that fetches user- or tenant-supplied URLs.

Rules: http/https only; the host must resolve only to public addresses (no private, loopback,
link-local, multicast, reserved, unspecified; IPv4-mapped IPv6 is unwrapped first); every
redirect hop is re-validated; at most 5 redirects; response bodies are capped at 5 MB.

Residual risk: we validate the resolved addresses, then httpx resolves again when connecting,
so a DNS-rebinding attacker could in theory answer differently the second time. Closing that
needs connecting to the validated IP (pinning); see ADR-004.
"""

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from urllib.parse import urljoin, urlsplit

import httpx

from core.ingest.errors import FetchError, ResponseTooLarge, TooManyRedirects, UnsafeUrl

MAX_REDIRECTS = 5
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

Resolver = Callable[[str], Awaitable[list[str]]]


async def system_resolver(host: str) -> list[str]:
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise FetchError(f"cannot resolve host {host!r}") from e
    return sorted({str(info[4][0]) for info in infos})


def is_public_ip(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value.split("%", 1)[0])  # drop IPv6 zone id
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or not ip.is_global
    )


async def validate_url(url: str, resolver: Resolver = system_resolver) -> None:
    """Raise UnsafeUrl unless url is http(s) and its host resolves only to public IPs."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise UnsafeUrl(f"scheme not allowed: {parts.scheme!r}")
    host = parts.hostname
    if not host:
        raise UnsafeUrl("URL has no host")
    try:  # IP literal: check directly, no DNS
        ipaddress.ip_address(host)
        addresses = [host]
    except ValueError:
        addresses = await resolver(host)
    if not addresses:
        raise UnsafeUrl(f"host {host!r} did not resolve")
    for addr in addresses:
        if not is_public_ip(addr):
            raise UnsafeUrl(f"host {host!r} resolves to a non-public address")


async def safe_get(
    client: httpx.AsyncClient,
    url: str,
    *,
    resolver: Resolver = system_resolver,
    max_bytes: int = MAX_RESPONSE_BYTES,
    max_redirects: int = MAX_REDIRECTS,
    headers: dict[str, str] | None = None,
) -> tuple[httpx.Response, bytes]:
    """GET with redirects followed manually (each hop validated) and a streamed size cap.

    The client must be created with follow_redirects=False (the default). Returns the final
    response (headers/status/url; body already consumed) and its body bytes.
    """
    current = url
    for _ in range(max_redirects + 1):
        await validate_url(current, resolver)
        async with client.stream("GET", current, headers=headers) as resp:
            if resp.status_code in _REDIRECT_STATUSES:
                location = resp.headers.get("location")
                if not location:
                    raise FetchError(f"redirect without Location from {current}")
                current = urljoin(str(resp.url), location)
                continue
            declared = resp.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > max_bytes:
                raise ResponseTooLarge(f"{current} declares {declared} bytes")
            body = bytearray()
            async for chunk in resp.aiter_bytes():
                body.extend(chunk)
                if len(body) > max_bytes:
                    raise ResponseTooLarge(f"{current} exceeds {max_bytes} bytes")
            return resp, bytes(body)
    raise TooManyRedirects(f"more than {max_redirects} redirects from {url}")
