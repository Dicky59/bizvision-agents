import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agents.assistant import repository as arepo
from api.deps import ApiDeps
from api.ratelimit import InMemoryLimiter
from core.leads import repository as repo
from core.leads.service import notify_pending
from core.notify.sender import EmailMessage, SendResult
from tests.api_helpers import ORIGIN, api_tenant, client_for, tenant_config

SM = async_sessionmaker[AsyncSession]
NO_WAIT = (0.0, 0.0)


class RecordingSender:
    def __init__(self, fail: bool = False) -> None:
        self.sent: list[EmailMessage] = []
        self.fail = fail

    async def send(self, message: EmailMessage) -> SendResult:
        self.sent.append(message)
        return SendResult(ok=not self.fail, provider_id=None if self.fail else "id1")


def lead_body(**kw: object) -> dict:
    return {
        "name": "Matti Meikäläinen",
        "email": "Matti@Example.com",
        "phone": "+358401234567",
        "message": "Soittakaa minulle.",
        "consent": True,
        "language": "fi",
        **kw,
    }


def make_deps(sm: SM, sender: RecordingSender, **kw: object) -> ApiDeps:
    return ApiDeps(sessionmaker=lambda: sm, sender=lambda: sender, notify_backoff=NO_WAIT, **kw)  # type: ignore[arg-type]


async def post(c, slug: str, payload: dict, origin: str | None = ORIGIN):
    headers = {"Origin": origin} if origin else {}
    return await c.post(f"/v1/assistant/{slug}/leads", json=payload, headers=headers)


async def test_lead_stored_and_notified_with_reply_to(sessionmaker: SM) -> None:
    sender = RecordingSender()
    async with api_tenant(sessionmaker) as t:
        before = datetime.now(UTC)
        async with client_for(make_deps(sessionmaker, sender)) as c:
            r = await post(
                c,
                t.slug,
                lead_body(
                    # client-supplied values for server-owned fields must be ignored
                    privacy_notice_version="fake",
                    consent_at="2000-01-01T00:00:00Z",
                ),
            )
        assert r.status_code == 201 and r.json() == {"status": "received"}
        assert r.headers["access-control-allow-origin"] == ORIGIN
        async with sessionmaker() as s:
            (lead,) = await repo.list_leads(s, t.id)
    assert lead.email == "Matti@example.com"  # domain part normalised by the validator
    assert lead.language == "fi"
    assert lead.privacy_notice_version == "v1"  # from tenant config, not the client
    assert before - timedelta(seconds=1) <= lead.consent_at <= datetime.now(UTC)
    assert lead.notified_at is not None
    (msg,) = sender.sent
    assert msg.to == "info@test.example.com" and msg.reply_to == "info@test.example.com"
    assert "Matti Meikäläinen" in msg.subject and "Soittakaa minulle." in msg.text


async def test_consent_is_required(sessionmaker: SM) -> None:
    sender = RecordingSender()
    async with api_tenant(sessionmaker) as t:
        async with client_for(make_deps(sessionmaker, sender)) as c:
            no = await post(c, t.slug, lead_body(consent=False))
            missing = await post(
                c, t.slug, {k: v for k, v in lead_body().items() if k != "consent"}
            )
            as_text = await post(c, t.slug, lead_body(consent="yes please"))
        async with sessionmaker() as s:
            leads = await repo.list_leads(s, t.id)
    assert no.status_code == missing.status_code == as_text.status_code == 422
    assert leads == [] and sender.sent == []


async def test_input_validation(sessionmaker: SM) -> None:
    async with api_tenant(sessionmaker) as t:
        async with client_for(make_deps(sessionmaker, RecordingSender())) as c:
            codes = [
                (await post(c, t.slug, lead_body(email="not-an-email"))).status_code,
                (await post(c, t.slug, lead_body(name="   "))).status_code,
                (await post(c, t.slug, lead_body(message="x" * 5001))).status_code,
                (await post(c, t.slug, lead_body(language="sv"))).status_code,
            ]
    assert codes == [422, 422, 422, 422]


async def test_honeypot_looks_like_success_but_stores_nothing(sessionmaker: SM) -> None:
    sender = RecordingSender()
    async with api_tenant(sessionmaker) as t:
        async with client_for(make_deps(sessionmaker, sender)) as c:
            r = await post(c, t.slug, lead_body(website="http://spam.example"))
        async with sessionmaker() as s:
            leads = await repo.list_leads(s, t.id)
    assert r.status_code == 201 and r.json() == {"status": "received"}
    assert leads == [] and sender.sent == []


async def test_origin_is_checked(sessionmaker: SM) -> None:
    sender = RecordingSender()
    async with api_tenant(sessionmaker) as t:
        async with client_for(make_deps(sessionmaker, sender)) as c:
            bad = await post(c, t.slug, lead_body(), origin="https://evil.example.org")
            none = await post(c, t.slug, lead_body(), origin=None)
    assert bad.status_code == none.status_code == 403 and sender.sent == []


async def test_rate_limit_five_per_hour_per_ip(sessionmaker: SM) -> None:
    async with api_tenant(sessionmaker) as t:
        deps = make_deps(sessionmaker, RecordingSender(), limiter=InMemoryLimiter())
        async with client_for(deps) as c:
            codes = [
                (await post(c, t.slug, lead_body(website="bot"))).status_code for _ in range(6)
            ]
            last = await post(c, t.slug, lead_body())
    assert codes == [201] * 5 + [429]  # honeypot hits count too
    assert int(last.headers["retry-after"]) > 0


async def test_foreign_conversation_id_is_not_attached(
    sessionmaker: SM, other_tenant_id: uuid.UUID
) -> None:
    async with api_tenant(sessionmaker) as t:
        async with sessionmaker() as s, s.begin():
            foreign = await arepo.get_or_create_conversation(
                s, other_tenant_id, "visitor-x", None, retention_days=90
            )
        async with client_for(make_deps(sessionmaker, RecordingSender())) as c:
            r = await post(c, t.slug, lead_body(conversation_id=str(foreign.id)))
        async with sessionmaker() as s:
            (lead,) = await repo.list_leads(s, t.id)
    assert r.status_code == 201 and lead.conversation_id is None


async def test_transcript_and_page_url_in_email_respect_tenant_setting(sessionmaker: SM) -> None:
    for include in (True, False):
        sender = RecordingSender()
        cfg: dict[str, Any] = {
            "handoff": {
                "email": "info@test.example.com",
                "privacy_notice_version": "v1",
                "include_transcript": include,
            }
        }
        async with api_tenant(sessionmaker, **cfg) as t:
            async with sessionmaker() as s, s.begin():
                conv = await arepo.get_or_create_conversation(
                    s,
                    t.id,
                    "visitor-1",
                    None,
                    retention_days=90,
                    page_url="https://shop.example.com/x",
                )
                await arepo.add_message(s, t.id, conv.id, "user", "Paljonko maksaa?")
            async with client_for(make_deps(sessionmaker, sender)) as c:
                await post(c, t.slug, lead_body(conversation_id=str(conv.id), language="en"))
        (msg,) = sender.sent
        assert msg.subject.startswith("New lead")  # English template for language=en
        assert "https://shop.example.com/x" in msg.text
        assert ("Paljonko maksaa?" in msg.text) is include


async def test_failed_send_keeps_lead_pending_and_notify_pending_retries(sessionmaker: SM) -> None:
    failing = RecordingSender(fail=True)
    async with api_tenant(sessionmaker) as t:
        async with client_for(make_deps(sessionmaker, failing)) as c:
            r = await post(c, t.slug, lead_body())
        assert r.status_code == 201  # the visitor is not told about mail trouble
        assert len(failing.sent) == 3  # 3 attempts
        async with sessionmaker() as s:
            (lead,) = await repo.list_leads(s, t.id)
        assert lead.notified_at is None

        cfg = tenant_config(t.slug)
        still = await notify_pending(sessionmaker, failing, t.id, cfg, backoff=NO_WAIT)
        assert (still.sent, still.failed) == (0, 1)

        working = RecordingSender()
        done = await notify_pending(sessionmaker, working, t.id, cfg, backoff=NO_WAIT)
        assert (done.sent, done.failed) == (1, 0) and len(working.sent) == 1
        async with sessionmaker() as s:
            assert await repo.list_unnotified(s, t.id) == []
        again = await notify_pending(sessionmaker, working, t.id, cfg, backoff=NO_WAIT)
        assert (again.sent, again.failed) == (0, 0)  # nothing is sent twice
