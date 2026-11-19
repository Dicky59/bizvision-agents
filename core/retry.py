"""Shared retry policy for outbound HTTP-ish calls (LLM, embeddings)."""

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from typing import Any

log = logging.getLogger(__name__)

RETRYABLE_STATUS = frozenset({429, 529})
MAX_RETRIES = 3
BASE_DELAY_S = 1.0
MAX_WAIT_S = 20.0


def is_retryable(exc: Exception, connection_errors: tuple[type[Exception], ...]) -> bool:
    if isinstance(exc, connection_errors):
        return True
    status = getattr(exc, "status_code", None)
    return isinstance(status, int) and (status in RETRYABLE_STATUS or status >= 500)


def retry_after_seconds(exc: Exception) -> float | None:
    """Value of the retry-after header (seconds form) if present and sane."""
    headers: Any = getattr(getattr(exc, "response", None), "headers", None)
    if headers is None:
        return None
    raw = headers.get("retry-after")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


async def retry_async[T](
    fn: Callable[[], Awaitable[T]],
    *,
    connection_errors: tuple[type[Exception], ...] = (),
    max_retries: int = MAX_RETRIES,
    base_delay: float = BASE_DELAY_S,
    max_wait: float = MAX_WAIT_S,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    jitter: Callable[[], float] = random.random,
) -> T:
    """Call fn, retrying retryable failures up to max_retries times.

    Wait = retry-after header when present (capped at max_wait), otherwise
    exponential backoff (base * 2**attempt) plus up to 1 s jitter, also capped.
    """
    attempt = 0
    while True:
        try:
            return await fn()
        except Exception as exc:
            if attempt >= max_retries or not is_retryable(exc, connection_errors):
                raise
            hinted = retry_after_seconds(exc)
            wait = hinted if hinted is not None else base_delay * 2**attempt + jitter()
            wait = min(wait, max_wait)
            log.warning(
                "retrying after %s (attempt %d/%d, wait %.1fs)",
                type(exc).__name__,
                attempt + 1,
                max_retries,
                wait,
            )
            await sleep(wait)
            attempt += 1
