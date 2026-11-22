"""Building the model-facing pieces: the <context> block, history, and citation extraction."""

import html
import re
from collections.abc import Sequence
from dataclasses import dataclass

from agents.assistant.types import SourceRef
from core.llm.types import Message
from core.rag.retrieve import RetrievedChunk

_TAG = re.compile(r"<(?=/?(?:doc|context|question|message|history)\b)", re.IGNORECASE)
_MARKER = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")


def neutralize_tags(text: str) -> str:
    """Defuse our delimiter tags inside untrusted text so it cannot close or fake a block."""
    return _TAG.sub("&lt;", text)


@dataclass(frozen=True)
class ContextDoc:
    id: int  # 1-based, as cited by the model
    title: str
    url: str | None
    content: str


def build_context(chunks: Sequence[RetrievedChunk]) -> tuple[str, dict[int, ContextDoc]]:
    docs: dict[int, ContextDoc] = {}
    parts: list[str] = []
    for i, c in enumerate(chunks, start=1):
        title = c.title or c.heading_path or "-"
        docs[i] = ContextDoc(id=i, title=title, url=c.url, content=c.content)
        attrs = f'id="{i}" title="{html.escape(title, quote=True)}"'
        if c.url:
            attrs += f' url="{html.escape(c.url, quote=True)}"'
        parts.append(f"<doc {attrs}>\n{neutralize_tags(c.content)}\n</doc>")
    return "<context>\n" + "\n".join(parts) + "\n</context>", docs


def extract_sources(text: str, docs: dict[int, ContextDoc]) -> list[SourceRef]:
    """Sources for ids that appear as [n] in the text AND exist in the context; web docs only
    (FAQ and file docs have no URL). Deduplicated by URL, in order of first citation."""
    by_url: dict[str, tuple[str, list[int]]] = {}  # insertion order = first citation
    for m in _MARKER.finditer(text):
        for raw in m.group(1).split(","):
            n = int(raw)
            doc = docs.get(n)
            if doc is None or doc.url is None:
                continue
            _, ns = by_url.setdefault(doc.url, (doc.title, []))
            if n not in ns:
                ns.append(n)
    return [SourceRef(title=t, url=u, ns=tuple(ns)) for u, (t, ns) in by_url.items()]


def recent_history(history: Sequence[Message], turns: int) -> list[Message]:
    """The last `turns` messages, shaped for the API: starts with a user message, roles
    alternate (consecutive same-role messages are merged) and ends with an assistant message
    (a dangling user message from a failed turn is dropped)."""
    recent = list(history[-turns:]) if turns > 0 else []
    while recent and recent[0].role != "user":
        recent.pop(0)
    out: list[Message] = []
    for m in recent:
        if out and out[-1].role == m.role:
            out[-1] = Message(m.role, out[-1].content + "\n" + m.content)
        else:
            out.append(m)
    if out and out[-1].role == "user":
        out.pop()
    return out


def history_text(history: Sequence[Message], turns: int) -> str:
    recent = list(history[-turns:]) if turns > 0 else []
    return "\n".join(f"{m.role}: {neutralize_tags(m.content)}" for m in recent)
