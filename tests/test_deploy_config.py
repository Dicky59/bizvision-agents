"""Guards for the production deployment files (client-IP layering, docs off, secrets out of git)."""

import re
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request

from api.main import create_app
from api.ratelimit import client_ip
from core.config import get_settings

ROOT = Path(__file__).resolve().parent.parent


def _request(peer: str, xff: str | None) -> Request:
    headers = [(b"x-forwarded-for", xff.encode())] if xff else []
    return Request({"type": "http", "headers": headers, "client": (peer, 1234)})


def test_spoofed_xff_cannot_choose_the_rate_limit_key() -> None:
    """Production layering: uvicorn does not rewrite the peer, the app takes the entry Caddy
    appended (rightmost). A client-supplied leftmost entry must never become the key."""
    caddy = "172.18.0.3"
    spoofed = "6.6.6.6, 7.7.7.7, 198.51.100.7"  # last entry is what Caddy wrote
    assert client_ip(_request(caddy, spoofed), 1) == "198.51.100.7"
    # Without a trusted proxy the header is ignored entirely, and the peer is Caddy's address.
    assert client_ip(_request(caddy, spoofed), 0) == caddy


def test_uvicorn_does_not_interpret_forwarded_headers() -> None:
    """Only one layer may interpret X-Forwarded-For: the app (TRUSTED_PROXY_COUNT).
    uvicorn defaults to --proxy-headers, so the image must switch it off explicitly."""
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    cmd = re.search(r"^CMD\s+(\[.*\])\s*$", dockerfile, re.MULTILINE)
    assert cmd is not None, "Dockerfile needs an exec-form CMD"
    assert '"--no-proxy-headers"' in cmd.group(1)
    assert "--proxy-headers" not in cmd.group(1).replace("--no-proxy-headers", "")
    assert "--workers" not in cmd.group(1), "the rate limiter is in-memory: one process only"


def test_caddy_is_the_only_proxy_in_front() -> None:
    """TRUSTED_PROXY_COUNT=1 assumes exactly one proxy. A CDN in front of Caddy (or a
    `trusted_proxies` setting) changes the count, so this test must be revisited with it."""
    caddyfile = (ROOT / "deploy" / "Caddyfile").read_text(encoding="utf-8")
    directives = "\n".join(
        line for line in caddyfile.splitlines() if not line.strip().startswith("#")
    )
    assert "trusted_proxies" not in directives
    env = (ROOT / "deploy" / "env.production.example").read_text(encoding="utf-8")
    assert re.search(r"^TRUSTED_PROXY_COUNT=1$", env, re.MULTILINE)


def test_remote_deploy_commands_cannot_eat_the_script() -> None:
    """The remote part of deploy.sh is fed to `bash -s` on stdin. `compose run` attaches stdin
    (also with -T), so without </dev/null it swallows the remaining steps and bash exits 0."""
    script = (ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    heredoc = re.search(r"<<'REMOTE'[^\n]*\n(.*?)^REMOTE$(.*)", script, re.DOTALL | re.MULTILINE)
    assert heredoc is not None
    remote, local = heredoc.groups()
    runs = [
        line for line in remote.splitlines() if re.search(r"(\$C|docker( compose)?) run ", line)
    ]
    assert runs, "expected at least the migration step"
    for line in runs:
        assert " -T " in line, line
        assert line.rstrip().endswith("</dev/null"), line
    # A truncated remote run must fail locally: the marker is the last remote command.
    assert remote.rstrip().splitlines()[-1] == 'echo "remote steps done"'
    assert "grep -qxF 'remote steps done'" in local


def test_production_env_template_has_no_secrets() -> None:
    env = (ROOT / "deploy" / "env.production.example").read_text(encoding="utf-8")
    for line in env.splitlines():
        name, _, value = line.partition("=")
        if name in {
            "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY",
            "RESEND_API_KEY",
            "ADMIN_API_KEY",
        }:
            assert value == "", f"{name} must be empty in the template"
    assert "npg_" not in env  # Neon password prefix


async def test_docs_are_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ENABLE_DOCS", raising=False)
    get_settings.cache_clear()
    try:
        app = create_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            for path in ("/docs", "/redoc", "/openapi.json"):
                assert (await c.get(path)).status_code == 404, path
    finally:
        get_settings.cache_clear()


async def test_docs_can_be_enabled_for_development(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENABLE_DOCS", "true")
    get_settings.cache_clear()
    try:
        app = create_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            assert (await c.get("/openapi.json")).status_code == 200
    finally:
        get_settings.cache_clear()
