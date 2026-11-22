"""Server-Sent Events encoding and a disconnect-safe event pump.

The assistant turn runs in its own task and feeds a queue; the HTTP response only drains the
queue. If the visitor closes the tab mid-answer the response generator is cancelled, but the turn
still finishes, so the assistant message is saved and the LLM usage recorded accurately (the
answer is bounded by max_output_tokens, so the cost of an abandoned turn is small).
"""

import asyncio
import json
import logging
from collections.abc import AsyncGenerator, AsyncIterator
from typing import Any

from agents.assistant.messages import static_message
from agents.assistant.types import Debug, Done, Error, Event, LeadForm, Meta, Sources, Token
from core.tenants.config import TenantConfig

log = logging.getLogger(__name__)

SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",  # tell nginx-style proxies not to buffer the stream
}

_running: set[asyncio.Task[None]] = set()  # strong refs, so unobserved turns are not collected


def frame(event: str, data: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


def encode(ev: Event) -> bytes | None:
    """Map an assistant event to its SSE frame; internal events (Debug) are never sent."""
    match ev:
        case Meta(conversation_id=cid):
            return frame("meta", {"conversation_id": str(cid)})
        case Token(text=text):
            return frame("token", {"text": text})
        case Sources(items=items):
            return frame(
                "sources",
                {"items": [{"ns": list(i.ns), "title": i.title, "url": i.url} for i in items]},
            )
        case LeadForm(reason=reason):
            return frame("lead_form", {"reason": reason})
        case Error(code=code, message=message):
            return frame("error", {"code": code, "message": message})
        case Done(message_id=mid):
            return frame("done", {"message_id": str(mid) if mid else None})
        case Debug():
            return None
    return None


async def stream_events(
    events: AsyncIterator[Event], cfg: TenantConfig
) -> AsyncGenerator[bytes, None]:
    queue: asyncio.Queue[bytes | None] = asyncio.Queue()

    async def pump() -> None:
        try:
            async for ev in events:
                data = encode(ev)
                if data is not None:
                    queue.put_nowait(data)
        except Exception as e:  # never leak details to the visitor; type only in the log
            log.error("chat stream failed: %s", type(e).__name__)
            msg = static_message(cfg, "unavailable", cfg.default_language)
            queue.put_nowait(frame("error", {"code": "internal", "message": msg}))
        finally:
            queue.put_nowait(None)

    task = asyncio.create_task(pump())
    _running.add(task)
    task.add_done_callback(_running.discard)
    while (item := await queue.get()) is not None:
        yield item
