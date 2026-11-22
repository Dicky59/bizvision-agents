import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.deps import ApiDeps
from api.ratelimit import InMemoryLimiter
from api.routers import admin
from core.db.models import Tenant, UsageEvent
from core.embeddings.client import EmbeddingClient
from core.leads import repository as lead_repo
from core.tenants.service import clear_cache
from tests.api_helpers import ORIGIN, api_tenant, client_for, tenant_config
from tests.fakes import FakeEmbeddingProvider
from tests.test_crawler import resolve_public
from tests.test_ingest_service import BASE, site_with

SM = async_sessionmaker[AsyncSession]
KEY = "k" * 40
AUTH = {"Authorization": f"Bearer {KEY}"}


def make_deps(sm: SM, **kw: Any) -> ApiDeps:
    provider = FakeEmbeddingProvider()
    site = site_with("/", "/palvelut")
    base: dict[str, Any] = {
        "sessionmaker": lambda: sm,
        "admin_key": lambda: KEY,
        "embedder": lambda: EmbeddingClient(provider, sm, 0.92),
        "crawl_client": lambda: httpx.AsyncClient(transport=httpx.MockTransport(site.handler)),
        "resolver": resolve_public,
        "crawl_delay": 0,
    }
    return ApiDeps(**{**base, **kw})


# ------------------------------------------------------------------ auth


async def test_disabled_without_key(sessionmaker: SM) -> None:
    async with client_for(make_deps(sessionmaker, admin_key=lambda: "")) as c:
        r = await c.get("/v1/admin/tenants/x", headers=AUTH)
    assert r.status_code == 503 and r.json() == {"error": {"code": "admin_disabled"}}


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong"},
        {"Authorization": KEY},
        {"Authorization": f"Basic {KEY}"},
    ],
)
async def test_bad_credentials_are_401(sessionmaker: SM, headers: dict[str, str]) -> None:
    async with api_tenant(sessionmaker) as t, client_for(make_deps(sessionmaker)) as c:
        r = await c.get(f"/v1/admin/tenants/{t.slug}", headers=headers)
    assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"
    assert t.slug not in r.text


async def test_failed_auth_is_throttled_but_valid_key_still_works(sessionmaker: SM) -> None:
    deps = make_deps(sessionmaker, limiter=InMemoryLimiter())
    async with api_tenant(sessionmaker) as t, client_for(deps) as c:
        bad = [
            (
                await c.get(f"/v1/admin/tenants/{t.slug}", headers={"Authorization": "Bearer x"})
            ).status_code
            for _ in range(11)
        ]
        ok = await c.get(f"/v1/admin/tenants/{t.slug}", headers=AUTH)
    assert bad == [401] * 10 + [429]
    assert ok.status_code == 200


async def test_admin_routes_have_no_cors(sessionmaker: SM) -> None:
    async with api_tenant(sessionmaker) as t, client_for(make_deps(sessionmaker)) as c:
        r = await c.get(f"/v1/admin/tenants/{t.slug}", headers={**AUTH, "Origin": ORIGIN})
    assert "access-control-allow-origin" not in r.headers


# ------------------------------------------------------------------ tenants


async def test_upsert_creates_then_updates_and_invalidates_cache(sessionmaker: SM) -> None:
    slug = f"adm-{uuid.uuid4().hex[:8]}"
    cfg = tenant_config(slug).model_dump(mode="json")
    try:
        async with client_for(make_deps(sessionmaker)) as c:
            created = await c.post("/v1/admin/tenants", json=cfg, headers=AUTH)
            before = await c.get(f"/v1/widget/{slug}/config", headers={"Origin": ORIGIN})
            cfg["assistant"]["greeting"] = {"fi": "Uusi tervehdys"}
            updated = await c.post("/v1/admin/tenants", json=cfg, headers=AUTH)
            after = await c.get(f"/v1/widget/{slug}/config", headers={"Origin": ORIGIN})
        assert created.json()["created"] is True and created.json()["public_key"].startswith("pk_")
        assert updated.json()["created"] is False
        assert before.json()["greeting"] == {"fi": "Hei!"}
        assert after.json()["greeting"] == {
            "fi": "Uusi tervehdys"
        }  # not served from the 60 s cache
    finally:
        async with sessionmaker() as s:
            await s.execute(delete(Tenant).where(Tenant.slug == slug))
            await s.commit()
        clear_cache()


async def test_upsert_rejects_invalid_config(sessionmaker: SM) -> None:
    async with client_for(make_deps(sessionmaker)) as c:
        r = await c.post("/v1/admin/tenants", json={"slug": "Bad Slug!"}, headers=AUTH)
    assert r.status_code == 422


async def test_overview_and_unknown_tenant(sessionmaker: SM) -> None:
    async with (
        api_tenant(sessionmaker, status="paused") as t,
        client_for(make_deps(sessionmaker)) as c,
    ):
        ok = await c.get(f"/v1/admin/tenants/{t.slug}", headers=AUTH)
        missing = await c.get("/v1/admin/tenants/nope", headers=AUTH)
    body = ok.json()
    assert ok.status_code == 200 and body["status"] == "paused"  # admins see paused tenants
    assert body["stats"]["sources"] == 0 and body["stats"]["latest_ingest"] is None
    assert body["usage_this_month"]["cost_eur"] == 0
    assert missing.status_code == 404 and missing.json() == {"error": {"code": "tenant_not_found"}}


# ------------------------------------------------------------------ ingest / files / faq


async def test_ingest_runs_in_background_and_is_visible_in_overview(sessionmaker: SM) -> None:
    async with api_tenant(sessionmaker, ingest={"start_urls": [BASE], "max_pages": 10}) as t:
        async with client_for(make_deps(sessionmaker)) as c:
            r = await c.post(f"/v1/admin/tenants/{t.slug}/ingest", headers=AUTH)
            overview = (await c.get(f"/v1/admin/tenants/{t.slug}", headers=AUTH)).json()
    assert r.status_code == 202 and r.json() == {"status": "started"}
    assert overview["stats"]["sources"] == 2
    assert overview["stats"]["latest_ingest"]["status"] == "done"
    assert overview["stats"]["latest_ingest"]["stats"]["new"] == 2
    assert t.id not in admin._crawling  # pyright: ignore[reportPrivateUsage]


async def test_ingest_conflict_and_missing_start_urls(sessionmaker: SM) -> None:
    async with api_tenant(sessionmaker, ingest={"start_urls": [BASE]}) as t:
        async with client_for(make_deps(sessionmaker)) as c:
            admin._crawling.add(t.id)  # pyright: ignore[reportPrivateUsage]
            try:
                busy = await c.post(f"/v1/admin/tenants/{t.slug}/ingest", headers=AUTH)
            finally:
                admin._crawling.discard(t.id)  # pyright: ignore[reportPrivateUsage]
    assert busy.status_code == 409
    async with api_tenant(sessionmaker) as t2, client_for(make_deps(sessionmaker)) as c:
        none = await c.post(f"/v1/admin/tenants/{t2.slug}/ingest", headers=AUTH)
    assert none.status_code == 422 and none.json()["error"]["code"] == "no_start_urls"


async def test_file_upload(sessionmaker: SM, monkeypatch: pytest.MonkeyPatch) -> None:
    text = "# Hinnasto\n\nAsiakaspalvelija maksaa sovitun kuukausimaksun. " * 5
    async with api_tenant(sessionmaker) as t, client_for(make_deps(sessionmaker)) as c:
        url = f"/v1/admin/tenants/{t.slug}/files"
        ok = await c.post(
            url, files={"file": ("../../hinnasto.md", text.encode(), "text/markdown")}, headers=AUTH
        )
        again = await c.post(
            url, files={"file": ("hinnasto.md", text.encode(), "text/markdown")}, headers=AUTH
        )
        bad = await c.post(
            url, files={"file": ("virus.exe", b"MZ", "application/octet-stream")}, headers=AUTH
        )
        monkeypatch.setattr(admin, "MAX_FILE_BYTES", 10)
        big = await c.post(
            url, files={"file": ("big.md", b"x" * 11, "text/markdown")}, headers=AUTH
        )
    assert ok.status_code == 200 and ok.json()["new"] == 1
    assert again.json()["unchanged"] == 1  # path stripped: same identity "hinnasto.md"
    assert bad.status_code == 415 and bad.json()["error"]["code"] == "unsupported_file"
    assert big.status_code == 413


async def test_faq_replace_and_validation(sessionmaker: SM) -> None:
    async with api_tenant(sessionmaker) as t, client_for(make_deps(sessionmaker)) as c:
        url = f"/v1/admin/tenants/{t.slug}/faq"
        two = await c.put(
            url,
            json=[{"q": "Hinta?", "a": "Sovitaan."}, {"q": "Kieli?", "a": "Suomi."}],
            headers=AUTH,
        )
        one = await c.put(url, json=[{"q": "Hinta?", "a": "Sovitaan."}], headers=AUTH)
        placeholder = await c.put(url, json=[{"q": "Hinta?", "a": "{{PRICE}} euroa"}], headers=AUTH)
        empty_q = await c.put(url, json=[{"q": "", "a": "x"}], headers=AUTH)
    assert two.json()["new"] == 2
    assert one.json()["removed"] == 1 and one.json()["unchanged"] == 1
    assert placeholder.status_code == 422 and "PRICE" in placeholder.json()["error"]["detail"]
    assert empty_q.status_code == 422


async def test_ingest_endpoints_are_tenant_scoped(sessionmaker: SM) -> None:
    async with api_tenant(sessionmaker) as a, api_tenant(sessionmaker) as b:
        async with client_for(make_deps(sessionmaker)) as c:
            await c.put(
                f"/v1/admin/tenants/{a.slug}/faq",
                json=[{"q": "Hinta?", "a": "Sovitaan."}],
                headers=AUTH,
            )
            other = (await c.get(f"/v1/admin/tenants/{b.slug}", headers=AUTH)).json()
    assert other["stats"]["sources"] == 0 and other["stats"]["chunks"] == 0


# ------------------------------------------------------------------ usage / leads


async def test_usage_by_month(sessionmaker: SM) -> None:
    async with api_tenant(sessionmaker) as t, api_tenant(sessionmaker) as other:
        async with sessionmaker() as s, s.begin():
            for tid, when, cost in [
                (t.id, datetime(2026, 10, 31, 23, 59, tzinfo=UTC), 1.0),
                (t.id, datetime(2026, 11, 1, 0, 0, tzinfo=UTC), 2.0),
                (t.id, datetime(2026, 11, 15, tzinfo=UTC), 0.5),
                (other.id, datetime(2026, 11, 15, tzinfo=UTC), 99.0),
            ]:
                s.add(
                    UsageEvent(
                        tenant_id=tid,
                        operation="chat",
                        provider="anthropic",
                        model="claude-haiku-5-5",
                        input_tokens=100,
                        output_tokens=10,
                        cost_eur=cost,
                        created_at=when,
                    )  # fmt: skip
                )
        async with client_for(make_deps(sessionmaker)) as c:
            nov = (
                await c.get(f"/v1/admin/tenants/{t.slug}/usage?month=2026-11", headers=AUTH)
            ).json()
            dec = (
                await c.get(f"/v1/admin/tenants/{t.slug}/usage?month=2026-12", headers=AUTH)
            ).json()
            bad = await c.get(f"/v1/admin/tenants/{t.slug}/usage?month=2026-13", headers=AUTH)
            cur = await c.get(f"/v1/admin/tenants/{t.slug}/usage", headers=AUTH)
    assert nov["cost_eur"] == pytest.approx(2.5) and nov["input_tokens"] == 200
    assert nov["by_operation"][0]["events"] == 2
    assert dec["cost_eur"] == 0 and dec["by_operation"] == []
    assert bad.status_code == 422 and cur.status_code == 200


async def test_leads_list_is_tenant_scoped(sessionmaker: SM) -> None:
    async with api_tenant(sessionmaker) as t, api_tenant(sessionmaker) as other:
        async with sessionmaker() as s, s.begin():
            for tid, email in [(t.id, "a@example.com"), (other.id, "secret@example.com")]:
                await lead_repo.create_lead(
                    s, tid, conversation_id=None, name="N", email=email, phone=None, message=None,
                    language="fi", consent_at=datetime.now(UTC), privacy_notice_version="v1",
                )  # fmt: skip
        async with client_for(make_deps(sessionmaker)) as c:
            r = await c.get(f"/v1/admin/tenants/{t.slug}/leads", headers=AUTH)
            paged = await c.get(f"/v1/admin/tenants/{t.slug}/leads?limit=1&offset=1", headers=AUTH)
            unauth = await c.get(f"/v1/admin/tenants/{t.slug}/leads")
    assert [x["email"] for x in r.json()] == ["a@example.com"]
    assert "secret@example.com" not in r.text and paged.json() == []
    assert unauth.status_code == 401
