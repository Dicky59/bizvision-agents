import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr, Field, StringConstraints, field_validator

from api.deps import Deps, PublicTenant, Session, ip_of
from api.ratelimit import enforce
from core.leads import repository as repo
from core.leads.service import notify_lead
from core.tenants.config import Lang

router = APIRouter(prefix="/v1/assistant", tags=["leads"])

LEADS_PER_HOUR = 5

Text = Annotated[str, StringConstraints(strip_whitespace=True)]


class LeadRequest(BaseModel):
    conversation_id: uuid.UUID | None = None
    name: Annotated[Text, Field(min_length=1, max_length=200)]
    email: Annotated[EmailStr, Field(max_length=320)]
    phone: Annotated[Text | None, Field(max_length=64)] = None
    message: Annotated[Text | None, Field(max_length=5000)] = None
    consent: bool
    language: Lang
    # Honeypot: hidden in the widget form, so only bots fill it.
    website: Annotated[str | None, Field(max_length=500)] = None

    @field_validator("consent")
    @classmethod
    def _consent_required(cls, v: bool) -> bool:
        if v is not True:
            raise ValueError("consent is required")
        return v


@router.post("/{slug}/leads")
async def create_lead(
    slug: str,
    body: LeadRequest,
    request: Request,
    background: BackgroundTasks,
    tenant: PublicTenant,
    deps: Deps,
    session: Session,
) -> JSONResponse:
    enforce(
        deps.limiter, [(f"lead:hour:{tenant.slug}:{ip_of(request, deps)}", LEADS_PER_HOUR, 3600)]
    )
    received = JSONResponse({"status": "received"}, status_code=201)
    if body.website:  # bot: answer exactly like a success, store and send nothing
        return received

    cfg = tenant.config
    lead = await repo.create_lead(
        session,
        tenant.id,
        conversation_id=body.conversation_id,
        name=body.name,
        email=str(body.email),
        phone=body.phone or None,
        message=body.message or None,
        language=body.language,
        consent_at=datetime.now(UTC),  # server clock, never the client's
        privacy_notice_version=cfg.handoff.privacy_notice_version,
    )
    await session.commit()
    background.add_task(
        notify_lead,
        deps.sessionmaker(),
        deps.sender(),
        tenant.id,
        cfg,
        lead.id,
        backoff=deps.notify_backoff,
    )
    return received
