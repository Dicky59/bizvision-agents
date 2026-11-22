"""CLI: bv leads list / notify-pending / delete and bv cleanup (against the test database)."""

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from typer.testing import CliRunner

from agents.assistant import repository as arepo
from cli import deps
from cli.deps import Deps
from cli.main import app
from cli.tenants import run_with_session, run_with_sessionmaker
from core.db.models import Tenant
from core.leads import repository as repo
from core.notify.sender import EmailMessage, SendResult
from tests.test_cli_commands import CONFIG

runner = CliRunner()
SM = async_sessionmaker[AsyncSession]


class Sender:
    def __init__(self) -> None:
        self.sent: list[EmailMessage] = []

    async def send(self, message: EmailMessage) -> SendResult:
        self.sent.append(message)
        return SendResult(ok=True, provider_id="id")


@dataclass
class Env:
    slug: str
    tenant_id: uuid.UUID
    sender: Sender

    def run(self, *args: str, ok: bool = True):
        r = runner.invoke(app, list(args))
        if ok:
            assert r.exit_code == 0, r.output
        return r

    def seed(self, email: str, *, age_days: int = 0, conversation: bool = False) -> uuid.UUID:
        async def go(sm: SM) -> uuid.UUID:
            async with sm() as s, s.begin():
                cid = None
                if conversation:
                    conv = await arepo.get_or_create_conversation(
                        s, self.tenant_id, "visitor-1", None, retention_days=90
                    )
                    await arepo.add_message(s, self.tenant_id, conv.id, "user", "hei")
                    cid = conv.id
                lead = await repo.create_lead(
                    s,
                    self.tenant_id,
                    conversation_id=cid,
                    name="Test Person",
                    email=email,
                    phone=None,
                    message=None,
                    language="fi",
                    consent_at=datetime.now(UTC),
                    privacy_notice_version="1",
                )
                lead.created_at = datetime.now(UTC) - timedelta(days=age_days)
                return lead.id

        return run_with_sessionmaker(go)

    def emails(self) -> list[str]:
        async def go(sm: SM) -> list[str]:
            async with sm() as s:
                return [x.email for x in await repo.list_leads(s, self.tenant_id)]

        return sorted(run_with_sessionmaker(go))


@pytest.fixture
def env(cli_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Env]:
    slug = f"cli-{uuid.uuid4().hex[:8]}"
    sender = Sender()
    monkeypatch.setattr(
        deps,
        "DEPS",
        Deps(
            make_embedder=lambda sm: None,  # type: ignore[arg-type,return-value]
            make_client=lambda: None,  # type: ignore[arg-type,return-value]
            resolver=None,  # type: ignore[arg-type]
            delay=0,
            make_sender=lambda: sender,
        ),
    )
    path = tmp_path / "t.yaml"
    path.write_text(CONFIG.format(slug=slug, max_pages=10), encoding="utf-8")
    r = runner.invoke(app, ["tenants", "create", slug, "--config", str(path)])
    assert r.exit_code == 0, r.output

    async def find(session: AsyncSession) -> uuid.UUID:
        return (await session.execute(select(Tenant.id).where(Tenant.slug == slug))).scalar_one()

    yield Env(slug, run_with_session(find), sender)

    async def cleanup(session: AsyncSession) -> None:
        await session.execute(delete(Tenant).where(Tenant.slug == slug))
        await session.commit()

    run_with_session(cleanup)


def test_list_shows_pending_and_sent(env: Env) -> None:
    env.seed("a@example.com")
    out = env.run("leads", "list", env.slug).output
    assert "PENDING" in out and "a@example.com" in out
    env.run("leads", "notify-pending", env.slug)
    assert "sent" in env.run("leads", "list", env.slug).output


def test_notify_pending_sends_once_with_reply_to(env: Env) -> None:
    env.seed("a@example.com")
    env.seed("b@example.com")
    out = env.run("leads", "notify-pending", env.slug).output
    assert "Sent 2, failed 0" in out
    assert {m.reply_to for m in env.sender.sent} == {"info@example.com"}
    again = env.run("leads", "notify-pending", env.slug).output
    assert "Sent 0, failed 0" in again and len(env.sender.sent) == 2


def test_delete_requires_confirmation_then_erases(env: Env) -> None:
    env.seed("anna@example.com", conversation=True)
    env.seed("keep@example.com")
    declined = env.run("leads", "delete", env.slug, "anna@example.com", ok=False)
    assert declined.exit_code != 0 and env.emails() == ["anna@example.com", "keep@example.com"]
    done = env.run("leads", "delete", env.slug, "ANNA@example.com", "--yes").output
    assert "Deleted 1 lead(s) and 1 conversation(s)" in done
    assert env.emails() == ["keep@example.com"]


def test_cleanup_deletes_expired_leads_only(env: Env) -> None:
    env.seed("old@example.com", age_days=400)  # default retention is 365 days
    env.seed("recent@example.com", age_days=10)
    out = env.run("cleanup", env.slug).output
    assert "deleted 0 conversation(s), 1 lead(s)" in out
    assert env.emails() == ["recent@example.com"]


def test_unknown_tenant_fails_cleanly(env: Env) -> None:
    r = env.run("leads", "list", "does-not-exist", ok=False)
    assert r.exit_code == 1 and "not found" in r.output


def test_usage_command(env: Env) -> None:
    out = env.run("usage", env.slug).output
    assert "(no usage)" in out and "of EUR 30.00 budget" in out
    bad = env.run("usage", env.slug, "--month", "2026-13", ok=False)
    assert bad.exit_code == 1 and "YYYY-MM" in bad.output
