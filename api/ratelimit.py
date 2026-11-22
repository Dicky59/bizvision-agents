"""Sliding-window rate limiter behind a protocol (in-memory now, Redis-capable later).

In-memory state is per process, which is fine for the single-instance phase 1 deployment.
"""

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from fastapi import Request

from api.errors import ApiError


@dataclass(frozen=True)
class Decision:
    allowed: bool
    retry_after: int = 0


class RateLimiter(Protocol):
    def hit(self, key: str, limit: int, window_s: int) -> Decision: ...


class InMemoryLimiter:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}

    def hit(self, key: str, limit: int, window_s: int) -> Decision:
        """Record one request unless the window is full (a rejected request is not counted)."""
        now = self._clock()
        q = self._hits.setdefault(key, deque())
        while q and now - q[0] >= window_s:
            q.popleft()
        if len(q) >= limit:
            return Decision(False, max(1, int(q[0] + window_s - now) + 1))
        q.append(now)
        return Decision(True)

    def prune(self, max_window_s: int = 86400) -> None:
        """Drop idle keys so memory does not grow with every IP ever seen."""
        now = self._clock()
        for k in [k for k, q in self._hits.items() if not q or now - q[-1] >= max_window_s]:
            del self._hits[k]


def client_ip(request: Request, trusted_proxy_count: int) -> str:
    """The client address. Behind `n` trusted proxies the real client is the n-th entry from the
    right of X-Forwarded-For (anything further left is client-controlled and spoofable)."""
    peer = request.client.host if request.client else "unknown"
    if trusted_proxy_count <= 0:
        return peer
    parts = [p.strip() for p in request.headers.get("x-forwarded-for", "").split(",") if p.strip()]
    if len(parts) < trusted_proxy_count:
        return peer
    return parts[-trusted_proxy_count]


def enforce(limiter: RateLimiter, rules: list[tuple[str, int, int]]) -> None:
    """Apply (key, limit, window_s) rules in order; the first full window raises 429."""
    for key, limit, window in rules:
        d = limiter.hit(key, limit, window)
        if not d.allowed:
            raise ApiError(429, "rate_limited", headers={"Retry-After": str(d.retry_after)})
