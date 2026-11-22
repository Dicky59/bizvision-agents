import uuid
from typing import Annotated
from urllib.parse import urlparse

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

from api.deps import ApiDeps, Deps, PublicTenant, ip_of
from api.errors import ApiError
from api.ratelimit import enforce
from api.sse import SSE_HEADERS, stream_events
from core.tenants.config import Lang

router = APIRouter(prefix="/v1/assistant", tags=["assistant"])

CHAT_PER_MINUTE = 20
CHAT_PER_DAY = 200


class ChatRequest(BaseModel):
    conversation_id: uuid.UUID | None = None
    visitor_id: Annotated[str, Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]
    message: Annotated[str, Field(min_length=1, max_length=10_000)]  # tenant limit checked later
    language: Lang | None = None
    page_url: Annotated[str | None, Field(max_length=2000)] = None

    @field_validator("message")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("message is blank")
        return v

    @field_validator("page_url")
    @classmethod
    def _http_only(cls, v: str | None) -> str | None:
        """Only http(s) page URLs are stored; anything else is dropped rather than rejected."""
        if v is None:
            return None
        p = urlparse(v)
        return v if p.scheme in ("http", "https") and p.netloc else None


def chat_rules(
    deps: ApiDeps, request: Request, slug: str, per_day: int
) -> list[tuple[str, int, int]]:
    ip = ip_of(request, deps)
    return [
        (f"chat:min:{slug}:{ip}", CHAT_PER_MINUTE, 60),
        (f"chat:day:{slug}:{ip}", CHAT_PER_DAY, 86400),
        (f"chat:tenant-day:{slug}", per_day, 86400),
    ]


@router.post("/{slug}/chat")
async def chat(
    slug: str, body: ChatRequest, request: Request, tenant: PublicTenant, deps: Deps
) -> StreamingResponse:
    cfg = tenant.config
    enforce(deps.limiter, chat_rules(deps, request, tenant.slug, cfg.limits.chat_requests_per_day))
    if len(body.message) > cfg.limits.max_message_chars:
        raise ApiError(413, "message_too_long")
    service = deps.chat_service()
    events = service.chat(
        tenant_id=tenant.id,
        cfg=cfg,
        visitor_id=body.visitor_id,
        message=body.message,
        conversation_id=body.conversation_id,
        language=body.language,
        page_url=body.page_url,
    )
    return StreamingResponse(
        stream_events(events, cfg), media_type="text/event-stream", headers=SSE_HEADERS
    )
