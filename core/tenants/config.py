from typing import Literal, Self
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

Lang = Literal["fi", "en"]
SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]{0,62}$"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RetrievalConfig(_Strict):
    top_k: int = Field(default=6, ge=1, le=20)
    min_relevance: float = Field(default=0.35, ge=0, le=1)


class MessagesConfig(_Strict):
    """Optional per-tenant wording for the assistant's static (non-LLM) replies."""

    dont_know: dict[Lang, str] = {}
    decline: dict[Lang, str] = {}


class AssistantConfig(_Strict):
    model: str = "claude-haiku-5-5"
    max_output_tokens: int = Field(default=600, ge=1, le=8192)
    temperature: float = Field(default=0.2, ge=0, le=1)
    tone: str = ""
    company_description: str = ""
    greeting: dict[Lang, str] = {}
    starter_questions: dict[Lang, list[str]] = {}
    retrieval: RetrievalConfig = RetrievalConfig()
    history_turns: int = Field(default=8, ge=0, le=50)
    messages: MessagesConfig = MessagesConfig()


class IngestConfig(_Strict):
    start_urls: list[str] = []
    use_sitemap: bool = True
    max_pages: int = Field(default=200, ge=1, le=5000)
    include_patterns: list[str] = []
    exclude_patterns: list[str] = []


class HandoffConfig(_Strict):
    email: EmailStr
    privacy_notice_url: dict[Lang, str] = {}
    privacy_notice_version: str
    # Whether the notification email carries a summary of the visitor's messages.
    include_transcript: bool = True
    # Leads are personal data: `bv cleanup` deletes leads older than this.
    lead_retention_days: int = Field(default=365, ge=1, le=3650)


class LimitsConfig(_Strict):
    monthly_budget_eur: float = Field(default=30, ge=0)
    messages_per_conversation: int = Field(default=40, ge=1)
    max_message_chars: int = Field(default=1500, ge=1)


class BrandingConfig(_Strict):
    primary_color: str = Field(default="#1f6f8b", pattern=r"^#[0-9a-fA-F]{6}$")
    position: Literal["bottom-right", "bottom-left"] = "bottom-right"
    launcher_label: dict[Lang, str] = {}


class TenantConfig(_Strict):
    slug: str = Field(pattern=SLUG_PATTERN)
    name: str = Field(min_length=1, max_length=200)
    allowed_origins: list[str] = []
    languages: list[Lang] = Field(default=["fi", "en"], min_length=1)
    default_language: Lang = "fi"
    assistant: AssistantConfig = AssistantConfig()
    ingest: IngestConfig = IngestConfig()
    handoff: HandoffConfig
    limits: LimitsConfig = LimitsConfig()
    retention_days: int = Field(default=90, ge=1, le=3650)
    branding: BrandingConfig = BrandingConfig()

    @field_validator("allowed_origins")
    @classmethod
    def _origins(cls, v: list[str]) -> list[str]:
        out = []
        for o in v:
            p = urlparse(o)
            if p.scheme not in ("http", "https") or not p.netloc or p.path not in ("", "/"):
                raise ValueError(f"invalid origin (expected scheme://host[:port]): {o}")
            out.append(o.rstrip("/").lower())
        return out

    @model_validator(mode="after")
    def _default_lang(self) -> Self:
        if self.default_language not in self.languages:
            raise ValueError("default_language must be one of languages")
        return self
