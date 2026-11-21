"""Request, understanding and event types of the assistant (the events map 1:1 to M5's SSE)."""

import uuid
from dataclasses import dataclass, field
from typing import Literal

from core.llm.types import Message
from core.tenants.config import Lang, TenantConfig

Intent = Literal["question", "contact_request", "smalltalk", "off_topic"]


@dataclass(frozen=True)
class AssistantRequest:
    tenant_id: uuid.UUID
    cfg: TenantConfig
    message: str
    history: list[Message] = field(default_factory=list)  # earlier turns, oldest first
    conversation_id: uuid.UUID | None = None
    language_hint: Lang | None = None


@dataclass(frozen=True)
class Understanding:
    intent: Intent
    language: Lang  # reply/retrieval language; "other" users get "en"
    other_language: bool
    wants_human: bool
    search_query: str
    parse_failed: bool = False


@dataclass(frozen=True)
class SourceRef:
    title: str
    url: str


@dataclass(frozen=True)
class Meta:
    """First event of a chat turn."""

    conversation_id: uuid.UUID


@dataclass(frozen=True)
class Error:
    """User-safe failure (already in the conversation language); details are only logged."""

    code: str
    message: str


@dataclass(frozen=True)
class Token:
    text: str


@dataclass(frozen=True)
class Sources:
    items: list[SourceRef]


@dataclass(frozen=True)
class LeadForm:
    reason: str


@dataclass(frozen=True)
class Debug:
    """Pipeline internals for `bv chat --show-debug` and the evals; never sent to visitors."""

    data: dict[str, object]


@dataclass(frozen=True)
class Done:
    text: str
    sources: list[SourceRef]
    intent: Intent
    language: Lang
    answerable: bool | None  # None when no answer step ran
    relevance: float | None
    header_failed: bool
    lead_form: bool
    message_id: uuid.UUID | None = None  # the persisted assistant message, set by ChatService


Event = Meta | Token | Sources | LeadForm | Debug | Error | Done
