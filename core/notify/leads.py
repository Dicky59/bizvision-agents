"""Rendering and sending the `new_lead` notification (tenant gets it, the visitor never does).

Sending to visitor-supplied addresses would make the form an abuse vector, so there is no
confirmation email. Reply-To is the tenant's handoff address, so replies reach a real inbox.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from core.notify.sender import EmailMessage, EmailSender, SendResult

log = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).parent / "templates"
TRANSCRIPT_MAX_MESSAGES = 12
TRANSCRIPT_MAX_CHARS = 400

_env = Environment(
    loader=FileSystemLoader(TEMPLATE_DIR),
    autoescape=select_autoescape(["html"]),
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
)


@dataclass(frozen=True)
class TranscriptLine:
    role: Literal["user", "assistant"]
    content: str


@dataclass(frozen=True)
class NewLead:
    tenant_name: str
    handoff_email: str
    language: Literal["fi", "en"]
    name: str
    email: str
    phone: str | None
    message: str | None
    page_url: str | None
    consent_at: datetime
    privacy_notice_version: str
    transcript: list[TranscriptLine] = field(default_factory=list)


def _clip(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= TRANSCRIPT_MAX_CHARS else text[: TRANSCRIPT_MAX_CHARS - 1] + "…"


def render_new_lead(lead: NewLead) -> EmailMessage:
    lines = [
        TranscriptLine(line.role, _clip(line.content))
        for line in lead.transcript[-TRANSCRIPT_MAX_MESSAGES:]
    ]
    ctx = {
        "lead": lead,
        "transcript": lines,
        "consent": lead.consent_at.strftime("%Y-%m-%d %H:%M UTC"),
    }
    base = f"{lead.language}/new_lead"
    return EmailMessage(
        to=lead.handoff_email,
        subject=_env.get_template(f"{base}.subject.txt").render(ctx).strip(),
        text=_env.get_template(f"{base}.txt").render(ctx),
        html=_env.get_template(f"{base}.html").render(ctx),
        reply_to=lead.handoff_email,
    )


Sleep = Callable[[float], Awaitable[None]]


async def send_new_lead(
    sender: EmailSender,
    lead: NewLead,
    *,
    attempts: int = 3,
    backoff: tuple[float, ...] = (1.0, 4.0),
    sleep: Sleep = asyncio.sleep,
) -> SendResult:
    """Send with retries; returns the last result (never raises on provider failure)."""
    message = render_new_lead(lead)
    result = SendResult(ok=False)
    for i in range(attempts):
        result = await sender.send(message)
        if result.ok:
            return result
        if i < attempts - 1:
            await sleep(backoff[min(i, len(backoff) - 1)])
    return result
