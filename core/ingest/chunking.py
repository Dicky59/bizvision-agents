"""Heading-aware chunking of extracted text (markdown-style headings)."""

import re
from dataclasses import dataclass

TARGET_TOKENS = 500
MAX_TOKENS = 800
OVERLAP_RATIO = 0.15

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")


@dataclass(frozen=True)
class ChunkDraft:
    ordinal: int
    content: str
    heading_path: str | None
    token_count: int


def estimate_tokens(text: str) -> int:
    """Fast approximation (chars / 4); exactness is not required."""
    return max(1, len(text) // 4) if text else 0


def embedding_text(title: str | None, heading_path: str | None, content: str) -> str:
    head = " — ".join(p for p in (title, heading_path) if p)
    return f"{head}\n\n{content}" if head else content


def _sections(text: str) -> list[tuple[str | None, str]]:
    """Split into (heading_path, body) sections following the heading hierarchy."""
    stack: list[tuple[int, str]] = []
    sections: list[tuple[str | None, list[str]]] = [(None, [])]
    for line in text.splitlines():
        m = _HEADING_RE.match(line)
        if m:
            level, title = len(m.group(1)), m.group(2)
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            sections.append((" > ".join(t for _, t in stack), []))
        else:
            sections[-1][1].append(line)
    return [(path, "\n".join(lines).strip()) for path, lines in sections if "".join(lines).strip()]


def _split_long(paragraph: str, max_chars: int) -> list[str]:
    """Break an oversized paragraph at sentence boundaries, then hard-cut as a last resort."""
    pieces: list[str] = []
    current = ""
    for sentence in re.split(r"(?<=[.!?])\s+", paragraph):
        while len(sentence) > max_chars:
            if current:
                pieces.append(current)
                current = ""
            pieces.append(sentence[:max_chars])
            sentence = sentence[max_chars:]
        if current and len(current) + 1 + len(sentence) > max_chars:
            pieces.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        pieces.append(current)
    return pieces


def _pack(body: str, target_chars: int, max_chars: int, overlap_chars: int) -> list[str]:
    paragraphs: list[str] = []
    for p in re.split(r"\n\s*\n", body):
        p = p.strip()
        if not p:
            continue
        paragraphs.extend(_split_long(p, max_chars) if len(p) > max_chars else [p])

    chunks: list[str] = []
    current = ""
    for p in paragraphs:
        if current and len(current) + 2 + len(p) > target_chars:
            chunks.append(current)
            tail = current[-overlap_chars:] if overlap_chars else ""
            # start the next chunk on a word boundary so the overlap doesn't begin mid-word
            tail = tail.split(" ", 1)[1] if " " in tail and len(current) > overlap_chars else tail
            current = f"{tail}\n\n{p}".strip() if tail else p
            if len(current) > max_chars:  # overlap pushed us over the hard cap
                current = p
        else:
            current = f"{current}\n\n{p}" if current else p
    if current:
        chunks.append(current)
    return chunks


def chunk_text(
    text: str,
    *,
    target_tokens: int = TARGET_TOKENS,
    max_tokens: int = MAX_TOKENS,
    overlap_ratio: float = OVERLAP_RATIO,
) -> list[ChunkDraft]:
    """Split by headings first, then by paragraphs (~target / max tokens, ~15 % overlap)."""
    target_chars, max_chars = target_tokens * 4, max_tokens * 4
    overlap_chars = int(target_chars * overlap_ratio)
    drafts: list[ChunkDraft] = []
    for path, body in _sections(text):
        for content in _pack(body, target_chars, max_chars, overlap_chars):
            drafts.append(
                ChunkDraft(
                    ordinal=len(drafts),
                    content=content,
                    heading_path=path,
                    token_count=estimate_tokens(content),
                )
            )
    return drafts
