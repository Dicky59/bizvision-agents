"""Email sending behind a small protocol, so tests and local runs need no Resend key.

`ResendSender` talks to the Resend REST API with httpx (no SDK). Failures never raise out of
`send`: the caller gets `SendResult(ok=False)` and the lead stays stored. Only ids and status
codes are logged, never addresses, subjects or response bodies.
"""

import logging
from dataclasses import dataclass
from typing import Protocol

import httpx

log = logging.getLogger(__name__)

RESEND_URL = "https://api.resend.com/emails"


@dataclass(frozen=True)
class EmailMessage:
    to: str
    subject: str
    text: str
    html: str | None = None
    reply_to: str | None = None


@dataclass(frozen=True)
class SendResult:
    ok: bool
    provider_id: str | None = None


class EmailSender(Protocol):
    async def send(self, message: EmailMessage) -> SendResult: ...


class NullSender:
    """Used when no API key is configured: nothing is sent, the caller sees ok=False."""

    async def send(self, message: EmailMessage) -> SendResult:
        log.warning("email skipped: RESEND_API_KEY is not set")
        return SendResult(ok=False)


class ResendSender:
    def __init__(
        self, api_key: str, email_from: str, http_client: httpx.AsyncClient | None = None
    ) -> None:
        self._api_key = api_key
        self._from = email_from
        self._client = http_client or httpx.AsyncClient(timeout=15.0)

    async def send(self, message: EmailMessage) -> SendResult:
        # Header values must be single-line (visitor-controlled text can end up in the subject).
        subject = " ".join(message.subject.split())
        payload: dict[str, object] = {
            "from": self._from,
            "to": [message.to],
            "subject": subject,
            "text": message.text,
        }
        if message.html:
            payload["html"] = message.html
        if message.reply_to:
            payload["reply_to"] = message.reply_to
        try:
            resp = await self._client.post(
                RESEND_URL, json=payload, headers={"Authorization": f"Bearer {self._api_key}"}
            )
        except httpx.HTTPError as e:
            log.error("resend request failed: %s", type(e).__name__)
            return SendResult(ok=False)
        if resp.status_code // 100 != 2:
            log.error("resend rejected the email: status=%s", resp.status_code)
            return SendResult(ok=False)
        try:
            provider_id = resp.json().get("id")
        except ValueError:
            provider_id = None
        return SendResult(
            ok=True, provider_id=provider_id if isinstance(provider_id, str) else None
        )


def make_sender(
    api_key: str, email_from: str, http_client: httpx.AsyncClient | None = None
) -> EmailSender:
    if not api_key:
        return NullSender()
    return ResendSender(api_key, email_from, http_client)
