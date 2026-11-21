"""The answer step's header gate (ADR-005) over the streamed model output."""

import json
import logging
from typing import Any

log = logging.getLogger(__name__)

MAX_HEADER_CHARS = 200


def _parse_header(line: str) -> bool | None:
    """True/False for a valid `{"answerable": bool}` header, None if malformed."""
    try:
        data: Any = json.loads(line.strip())
    except json.JSONDecodeError:
        return None
    if isinstance(data, dict) and isinstance(data.get("answerable"), bool):
        return data["answerable"]
    return None


class HeaderGate:
    """Feed streamed text in, get the visitor-visible text out.

    The first line must be `{"answerable": true|false}`. `false` sets `rejected` (the caller
    must stop consuming the stream; nothing was emitted). A malformed header, or none within
    MAX_HEADER_CHARS, is treated as answerable and the buffered text is released as the start
    of the answer (`header_failed` is set so evals can count it).
    """

    def __init__(self) -> None:
        self._buf = ""
        self._in_header = True
        self._first_body = True
        self.rejected = False
        self.header_failed = False

    def _release(self, text: str) -> str:
        if self._first_body:
            text = text.lstrip("\n")
            if text:
                self._first_body = False
        return text

    def _fail(self, text: str) -> str:
        self.header_failed = True
        self._in_header = False
        self._first_body = False
        log.warning("answer: malformed or missing answerable header; treating as answerable")
        return text

    def feed(self, text: str) -> str:
        if self.rejected:
            return ""
        if not self._in_header:
            return self._release(text)
        self._buf += text
        stripped = self._buf.lstrip()
        if "\n" in stripped:
            line, rest = stripped.split("\n", 1)
            verdict = _parse_header(line)
            if verdict is None:
                return self._fail(self._buf)
            self._in_header = False
            if verdict is False:
                self.rejected = True
                return ""
            return self._release(rest)
        if len(stripped) > MAX_HEADER_CHARS:
            return self._fail(self._buf)
        return ""

    def finish(self) -> str:
        """Call once the stream ended; handles a header-only or short unterminated output."""
        if not self._in_header or self.rejected:
            return ""
        stripped = self._buf.lstrip()
        verdict = _parse_header(stripped) if stripped else None
        if verdict is None:
            return self._fail(self._buf)
        self._in_header = False
        if verdict is False:
            self.rejected = True
        return ""
