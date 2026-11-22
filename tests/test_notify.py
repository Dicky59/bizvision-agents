import json
from datetime import UTC, datetime

import httpx

from core.notify.leads import NewLead, TranscriptLine, render_new_lead, send_new_lead
from core.notify.sender import (
    EmailMessage,
    NullSender,
    ResendSender,
    SendResult,
    make_sender,
)

FROM = "BizVision AI <ai@notify.bizvision.fi>"


def make_lead(**kw: object) -> NewLead:
    base: dict[str, object] = {
        "tenant_name": "BizVision Oy",
        "handoff_email": "info@bizvision.fi",
        "language": "fi",
        "name": "Matti Meikäläinen",
        "email": "matti@example.com",
        "phone": "+358401234567",
        "message": "Haluaisin tarjouksen.",
        "page_url": "https://www.bizvision.fi/hinnat",
        "consent_at": datetime(2026, 11, 22, 10, 30, tzinfo=UTC),
        "privacy_notice_version": "2026-11",
        "transcript": [
            TranscriptLine("user", "Paljonko maksaa?"),
            TranscriptLine("assistant", "En löydä hintaa [1]."),
        ],
    }
    return NewLead(**{**base, **kw})  # type: ignore[arg-type]


def test_render_fi_sets_reply_to_and_content() -> None:
    msg = render_new_lead(make_lead())
    assert msg.to == "info@bizvision.fi"
    assert msg.reply_to == "info@bizvision.fi"
    assert "Matti Meikäläinen" in msg.subject
    assert "Puhelin: +358401234567" in msg.text
    assert "Kävijä: Paljonko maksaa?" in msg.text
    assert "2026-11-22 10:30 UTC" in msg.text and "2026-11" in msg.text
    assert msg.html and "mailto:matti@example.com" in msg.html


def test_render_en_and_optional_parts_omitted() -> None:
    msg = render_new_lead(
        make_lead(language="en", phone=None, message=None, page_url=None, transcript=[])
    )
    assert msg.subject.startswith("New lead")
    assert "Phone" not in msg.text and "Conversation summary" not in msg.text
    assert "Visitor" not in msg.text


def test_html_escapes_hostile_fields_text_does_not() -> None:
    evil = '<script>alert(1)</script> "x"'
    msg = render_new_lead(make_lead(name=evil, message=evil, transcript=[]))
    assert msg.html and "<script>" not in msg.html
    assert "&lt;script&gt;" in msg.html


def test_transcript_is_clipped_and_limited() -> None:
    lines = [TranscriptLine("user", f"m{i} " + "x" * 1000) for i in range(30)]
    msg = render_new_lead(make_lead(transcript=lines))
    assert "m29" in msg.text and "m17 " not in msg.text  # only the last 12 messages
    assert "…" in msg.text and "x" * 500 not in msg.text


async def test_resend_request_shape() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={"id": "re_123"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sender = ResendSender("re_key", FROM, client)
    res = await sender.send(
        EmailMessage(
            to="info@bizvision.fi",
            subject="Hi\r\nBcc: evil@example.com",
            text="t",
            html="<p>t</p>",
            reply_to="info@bizvision.fi",
        )
    )
    assert res == SendResult(ok=True, provider_id="re_123")
    (req,) = seen
    assert str(req.url) == "https://api.resend.com/emails"
    assert req.headers["authorization"] == "Bearer re_key"
    body = json.loads(req.content)
    assert body["from"] == FROM and body["to"] == ["info@bizvision.fi"]
    assert body["reply_to"] == "info@bizvision.fi"
    assert "\n" not in body["subject"] and "\r" not in body["subject"]
    assert body["html"] == "<p>t</p>"


async def test_resend_failure_returns_not_ok() -> None:
    for transport in (
        httpx.MockTransport(lambda r: httpx.Response(422, json={"message": "bad"})),
        httpx.MockTransport(lambda r: (_ for _ in ()).throw(httpx.ConnectError("down"))),
    ):
        sender = ResendSender("k", FROM, httpx.AsyncClient(transport=transport))
        assert (await sender.send(EmailMessage("a@b.fi", "s", "t"))).ok is False


async def test_null_sender_without_key() -> None:
    sender = make_sender("", FROM)
    assert isinstance(sender, NullSender)
    assert (await sender.send(EmailMessage("a@b.fi", "s", "t"))).ok is False


class FlakySender:
    def __init__(self, fail_times: int) -> None:
        self.calls: list[EmailMessage] = []
        self._fail = fail_times

    async def send(self, message: EmailMessage) -> SendResult:
        self.calls.append(message)
        if len(self.calls) <= self._fail:
            return SendResult(ok=False)
        return SendResult(ok=True, provider_id="id")


async def test_send_new_lead_retries_with_backoff() -> None:
    sleeps: list[float] = []

    async def sleep(s: float) -> None:
        sleeps.append(s)

    sender = FlakySender(fail_times=2)
    res = await send_new_lead(sender, make_lead(), sleep=sleep)
    assert res.ok and len(sender.calls) == 3 and sleeps == [1.0, 4.0]


async def test_send_new_lead_gives_up_after_attempts() -> None:
    async def sleep(s: float) -> None:
        pass

    sender = FlakySender(fail_times=99)
    res = await send_new_lead(sender, make_lead(), sleep=sleep)
    assert res.ok is False and len(sender.calls) == 3
