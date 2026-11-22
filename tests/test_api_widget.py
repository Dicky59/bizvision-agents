from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.deps import ApiDeps
from tests.api_helpers import ORIGIN, api_tenant, client_for

SM = async_sessionmaker[AsyncSession]
EVIL = "https://evil.example.org"


async def test_config_for_allowed_origin(sessionmaker: SM) -> None:
    async with api_tenant(sessionmaker) as t:
        async with client_for(ApiDeps(sessionmaker=lambda: sessionmaker)) as c:
            r = await c.get(f"/v1/widget/{t.slug}/config", headers={"Origin": ORIGIN})
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == ORIGIN
    assert "origin" in r.headers["vary"].lower()
    assert r.headers["access-control-expose-headers"] == "Retry-After"  # widget reads it on 429
    body = r.json()
    assert body["greeting"] == {"fi": "Hei!"}
    assert body["privacy_notice_version"] == "v1"
    assert body["display_name"] == body["name"]  # no branding.display_name -> falls back
    assert body["max_message_chars"] == 1500
    # whitelist: no handoff address, no model, no budget
    text = r.text
    assert "info@test.example.com" not in text and "monthly_budget" not in text
    assert "claude" not in text


async def test_rejections_are_identical(sessionmaker: SM) -> None:
    deps = ApiDeps(sessionmaker=lambda: sessionmaker)
    async with api_tenant(sessionmaker) as t, api_tenant(sessionmaker, status="paused") as paused:
        async with client_for(deps) as c:
            wrong = await c.get(f"/v1/widget/{t.slug}/config", headers={"Origin": EVIL})
            missing = await c.get(f"/v1/widget/{t.slug}/config")
            unknown = await c.get("/v1/widget/nope/config", headers={"Origin": ORIGIN})
            pause = await c.get(f"/v1/widget/{paused.slug}/config", headers={"Origin": ORIGIN})
    for r in (wrong, missing, unknown, pause):
        assert r.status_code == 403
        assert r.json() == {"error": {"code": "forbidden"}}
    assert "access-control-allow-origin" not in wrong.headers


async def test_preflight(sessionmaker: SM) -> None:
    deps = ApiDeps(sessionmaker=lambda: sessionmaker)
    async with api_tenant(sessionmaker) as t:
        async with client_for(deps) as c:
            ok = await c.options(
                f"/v1/assistant/{t.slug}/chat",
                headers={"Origin": ORIGIN, "Access-Control-Request-Method": "POST"},
            )
            bad = await c.options(
                f"/v1/assistant/{t.slug}/chat",
                headers={"Origin": EVIL, "Access-Control-Request-Method": "POST"},
            )
    assert ok.status_code == 204
    assert ok.headers["access-control-allow-origin"] == ORIGIN
    assert "POST" in ok.headers["access-control-allow-methods"]
    assert "access-control-allow-origin" not in bad.headers


async def test_healthz_needs_no_origin() -> None:
    async with client_for(ApiDeps()) as c:
        assert (await c.get("/healthz")).status_code == 200


async def test_widget_script_served_with_cache_header(tmp_path: Path) -> None:
    (tmp_path / "widget.js").write_text("(()=>{})();", encoding="utf-8")
    async with client_for(ApiDeps(widget_dist=tmp_path)) as c:
        r = await c.get("/widget.v1.js")  # no Origin: a script tag does not send one
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/javascript")
    assert r.headers["cache-control"] == "public, max-age=86400"
    assert r.text == "(()=>{})();"


async def test_widget_script_404_when_not_built(tmp_path: Path) -> None:
    async with client_for(ApiDeps(widget_dist=tmp_path)) as c:
        r = await c.get("/widget.v1.js")
    assert r.status_code == 404
    assert r.json() == {"error": {"code": "widget_not_built"}}
