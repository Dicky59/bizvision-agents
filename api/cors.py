"""Per-tenant CORS as a pure ASGI middleware (streaming-safe).

The allowed origins depend on the tenant in the URL (`/v1/<widget|assistant>/{tenant}/...`), so
Starlette's static CORSMiddleware does not fit. Only an origin listed in the tenant's
`allowed_origins` gets `Access-Control-Allow-Origin`; everything else gets no CORS headers (the
route's own origin check then rejects it). Preflights are answered here.
"""

import re
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from starlette.types import ASGIApp, Receive, Scope, Send

from core.tenants.service import get_tenant, origin_allowed

OriginCheck = Callable[[str, str], Awaitable[bool]]  # (tenant slug, origin) -> allowed

_PATH = re.compile(r"^/v1/(?:widget|assistant)/([^/]+)/")
ALLOW_METHODS = b"GET, POST, OPTIONS"
ALLOW_HEADERS = b"content-type"
MAX_AGE = b"600"


class TenantCORSMiddleware:
    def __init__(self, app: ASGIApp, origin_check: OriginCheck) -> None:
        self.app = app
        self._check = origin_check

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        m = _PATH.match(scope["path"])
        origin = next((v.decode("latin-1") for k, v in scope["headers"] if k == b"origin"), None)
        if not m or origin is None or not await self._check(m.group(1), origin):
            await self.app(scope, receive, send)
            return

        cors = [(b"access-control-allow-origin", origin.encode("latin-1")), (b"vary", b"Origin")]
        if scope["method"] == "OPTIONS":
            await send(
                {
                    "type": "http.response.start",
                    "status": 204,
                    "headers": [
                        *cors,
                        (b"access-control-allow-methods", ALLOW_METHODS),
                        (b"access-control-allow-headers", ALLOW_HEADERS),
                        (b"access-control-max-age", MAX_AGE),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": b""})
            return

        async def send_with_cors(message: MutableMapping[str, Any]) -> None:
            if message["type"] == "http.response.start":
                message["headers"] = [*message.get("headers", []), *cors]
            await send(message)

        await self.app(scope, receive, send_with_cors)


def make_origin_check(sessionmaker: Callable[[], Any]) -> OriginCheck:
    """`sessionmaker` is a zero-arg callable returning the async_sessionmaker (resolved lazily)."""

    async def check(slug: str, origin: str) -> bool:
        async with sessionmaker()() as session:
            tenant = await get_tenant(session, slug)
        return (
            tenant is not None
            and tenant.status == "active"
            and origin_allowed(tenant.config, origin)
        )

    return check
