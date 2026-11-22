"""Admin API (Authorization: Bearer ADMIN_API_KEY). Thin wrappers over core services.

Fail closed: with no ADMIN_API_KEY configured every admin route answers 503. Failed
authentications are throttled per IP. These routes return personal data (leads): never log bodies.
"""

import hmac
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, File, Query, Request, UploadFile
from pydantic import BaseModel, Field

from api.deps import ApiDeps, Deps, Session, ip_of
from api.errors import ApiError
from api.ratelimit import enforce
from core.db.models import Tenant
from core.ingest.errors import FileTooLarge, IngestError, UnsupportedFile
from core.ingest.files import MAX_FILE_BYTES
from core.ingest.service import FaqEntry, crawl_tenant, ingest_file, replace_faq
from core.leads import repository as lead_repo
from core.tenants.config import TenantConfig
from core.tenants.repository import get_by_slug, upsert_tenant
from core.tenants.service import clear_cache
from core.tenants.stats import tenant_stats
from core.usage import monthly_usage

log = logging.getLogger(__name__)

AUTH_FAILURES_PER_MINUTE = 10


async def require_admin(request: Request, deps: Deps) -> None:
    key = deps.admin_key()
    if not key:
        raise ApiError(503, "admin_disabled")
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() == "bearer" and hmac.compare_digest(token.encode(), key.encode()):
        return
    enforce(deps.limiter, [(f"admin-fail:{ip_of(request, deps)}", AUTH_FAILURES_PER_MINUTE, 60)])
    raise ApiError(401, "unauthorized", headers={"WWW-Authenticate": "Bearer"})


router = APIRouter(prefix="/v1/admin", tags=["admin"], dependencies=[Depends(require_admin)])


async def admin_tenant(slug: str, session: Session) -> Tenant:
    tenant = await get_by_slug(session, slug)  # no cache, no status filter: admins see paused too
    if tenant is None:
        raise ApiError(404, "tenant_not_found")
    return tenant


AdminTenant = Annotated[Tenant, Depends(admin_tenant)]


# ------------------------------------------------------------------ tenants


@router.post("/tenants")
async def upsert(config: TenantConfig, session: Session) -> dict[str, Any]:
    tenant, created = await upsert_tenant(session, config)
    clear_cache()  # visitors must see the new config now, not after the 60 s cache TTL
    return {"slug": tenant.slug, "created": created, "public_key": tenant.public_key}


@router.get("/tenants/{slug}")
async def get_tenant_overview(tenant: AdminTenant, session: Session) -> dict[str, Any]:
    stats = await tenant_stats(session, tenant.id)
    usage = await monthly_usage(session, tenant.id)
    latest = stats.latest_ingest
    return {
        "slug": tenant.slug,
        "name": tenant.name,
        "status": tenant.status,
        "public_key": tenant.public_key,
        "config": tenant.config,
        "stats": {
            "sources": stats.sources,
            "chunks": stats.chunks,
            "conversations": stats.conversations,
            "leads": stats.leads,
            "leads_pending_notification": stats.leads_pending,
            "latest_ingest": None
            if latest is None
            else {
                "kind": latest.kind,
                "status": latest.status,
                "started_at": latest.started_at.isoformat(),
                "stats": latest.stats,
            },
        },
        "usage_this_month": {"month": usage.month, "cost_eur": usage.cost_eur},
    }


# ------------------------------------------------------------------ ingest

_crawling: set[uuid.UUID] = set()


async def _run_crawl(deps: ApiDeps, tenant_id: uuid.UUID, cfg: TenantConfig) -> None:
    try:
        async with deps.crawl_client() as client:
            await crawl_tenant(
                deps.sessionmaker(),
                deps.embedder(),
                tenant_id,
                cfg.ingest,
                client,
                resolver=deps.resolver,
                delay=deps.crawl_delay,
            )
    except Exception as e:  # the job row records the failure; the log gets the type only
        log.error("crawl failed tenant=%s: %s", tenant_id, type(e).__name__)
    finally:
        _crawling.discard(tenant_id)


@router.post("/tenants/{slug}/ingest", status_code=202)
async def start_ingest(
    tenant: AdminTenant, deps: Deps, background: BackgroundTasks
) -> dict[str, str]:
    cfg = TenantConfig.model_validate(tenant.config)
    if not cfg.ingest.start_urls:
        raise ApiError(422, "no_start_urls")
    if tenant.id in _crawling:
        raise ApiError(409, "ingest_already_running")
    deps.embedder()  # fail with 503 now, not silently in the background
    _crawling.add(tenant.id)
    background.add_task(_run_crawl, deps, tenant.id, cfg)
    return {"status": "started"}


@router.post("/tenants/{slug}/files")
async def upload_file(
    tenant: AdminTenant, deps: Deps, file: Annotated[UploadFile, File()]
) -> dict[str, Any]:
    name = Path(file.filename or "").name  # never trust a client path
    if not name:
        raise ApiError(422, "filename_required")
    data = await file.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise ApiError(413, "file_too_large")
    try:
        stats = await ingest_file(deps.sessionmaker(), deps.embedder(), tenant.id, name, data)
    except FileTooLarge as e:
        raise ApiError(413, "file_too_large", detail=str(e)) from e
    except UnsupportedFile as e:
        raise ApiError(415, "unsupported_file", detail=str(e)) from e
    except IngestError as e:
        raise ApiError(422, "ingest_error", detail=str(e)) from e
    return stats.to_dict()


class FaqItem(BaseModel):
    q: Annotated[str, Field(min_length=1, max_length=500)]
    a: Annotated[str, Field(min_length=1, max_length=5000)]


@router.put("/tenants/{slug}/faq")
async def put_faq(
    tenant: AdminTenant, deps: Deps, items: Annotated[list[FaqItem], Field(max_length=500)]
) -> dict[str, Any]:
    entries = [FaqEntry(i.q, i.a) for i in items]
    try:
        stats = await replace_faq(deps.sessionmaker(), deps.embedder(), tenant.id, entries)
    except IngestError as e:
        raise ApiError(422, "invalid_faq", detail=str(e)) from e
    return stats.to_dict()


# ------------------------------------------------------------------ usage and leads


@router.get("/tenants/{slug}/usage")
async def usage(
    tenant: AdminTenant,
    session: Session,
    month: Annotated[str | None, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")] = None,
) -> dict[str, Any]:
    u = await monthly_usage(session, tenant.id, month)
    return {
        "month": u.month,
        "cost_eur": u.cost_eur,
        "input_tokens": u.input_tokens,
        "output_tokens": u.output_tokens,
        "by_operation": [
            {
                "operation": r.operation,
                "model": r.model,
                "events": r.events,
                "input_tokens": r.input_tokens,
                "output_tokens": r.output_tokens,
                "cost_eur": r.cost_eur,
            }
            for r in u.rows
        ],
    }


class LeadOut(BaseModel):
    id: uuid.UUID
    created_at: datetime
    name: str
    email: str
    phone: str | None
    message: str | None
    language: str | None
    conversation_id: uuid.UUID | None
    consent_at: datetime
    privacy_notice_version: str
    notified_at: datetime | None


@router.get("/tenants/{slug}/leads")
async def list_leads(
    tenant: AdminTenant,
    session: Session,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[LeadOut]:
    rows = await lead_repo.list_leads(session, tenant.id, limit=limit, offset=offset)
    return [LeadOut.model_validate(r, from_attributes=True) for r in rows]
