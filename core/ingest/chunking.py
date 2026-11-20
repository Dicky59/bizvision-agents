"""Heading-aware chunking of extracted text (markdown-style headings).

1. The text is split into sections, one per heading (plus the text before the first heading).
2. Small sections are merged: consecutive sections are grouped until the group reaches the target
   size (never beyond the maximum). A group never crosses a higher-level heading, so only
   siblings (and descendants) under the same parent are merged. A merged chunk keeps the
   sub-headings as text lines and its `heading_path` is the common parent path.
3. A section that is larger than the target on its own is split by paragraphs (with overlap).
"""

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


@dataclass(frozen=True)
class _Section:
    level: int  # heading level 1..6; text before the first heading takes the first heading's level
    path: tuple[str, ...]  # titles from the top-level heading down to this one; () before any
    title: str | None
    body: str


def estimate_tokens(text: str) -> int:
    """Fast approximation (chars / 4); exactness is not required."""
    return max(1, len(text) // 4) if text else 0


def embedding_text(title: str | None, heading_path: str | None, content: str) -> str:
    head = " — ".join(p for p in (title, heading_path) if p)
    return f"{head}\n\n{content}" if head else content


def _parse_sections(text: str) -> list[_Section]:
    """Split into one section per heading, following the heading hierarchy."""
    stack: list[tuple[int, str]] = []
    raw: list[tuple[int, str | None, tuple[str, ...], list[str]]] = [(0, None, (), [])]
    for line in text.splitlines():
        m = _HEADING_RE.match(line)
        if m:
            level, title = len(m.group(1)), m.group(2)
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            raw.append((level, title, tuple(t for _, t in stack), []))
        else:
            raw[-1][3].append(line)
    first_level = next((lvl for lvl, *_ in raw[1:]), 1)
    return [
        _Section(level or first_level, path, title, "\n".join(lines).strip())
        for level, title, path, lines in raw
        # keep bodyless headings (they give merged chunks their sub-heading lines); drop an
        # empty preamble
        if title is not None or "".join(lines).strip()
    ]


def _size(section: _Section) -> int:
    return len(section.body) + (len(section.title) + 6 if section.title else 0)


def _group_sections(
    sections: list[_Section], target_chars: int, max_chars: int
) -> list[list[_Section]]:
    """Merge consecutive sections while the group is below target and stays within max.

    - A section with a lower level number than anything in the group is a higher-level heading
      and closes the group.
    - Sections only merge when they share a parent heading (a non-empty common heading path), so
      unrelated top-level sections are never merged and `heading_path` stays accurate.
    """
    groups: list[list[_Section]] = []
    current: list[_Section] = []
    size = 0
    min_level = 0
    for section in sections:
        n = _size(section)
        if (
            current
            and section.level >= min_level
            and size < target_chars
            and size + n <= max_chars
            and _common_path([*current, section])
        ):
            current.append(section)
            size += n
            min_level = min(min_level, section.level)
        else:
            if current:
                groups.append(current)
            current, size, min_level = [section], n, section.level
    if current:
        groups.append(current)
    return groups


def _common_path(sections: list[_Section]) -> tuple[str, ...]:
    """Longest common heading path of the titled sections (text before the first heading is
    ignored: it belongs to whatever heading follows it)."""
    paths = [s.path for s in sections if s.title is not None]
    if not paths:
        return ()
    common = paths[0]
    for p in paths[1:]:
        i = 0
        while i < min(len(common), len(p)) and common[i] == p[i]:
            i += 1
        common = common[:i]
    return common


def _render_merged(sections: list[_Section], path: tuple[str, ...]) -> str:
    parts: list[str] = []
    for s in sections:
        if s.title is not None and s.path != path:  # the parent's own title is the heading_path
            parts.append(f"{'#' * s.level} {s.title}")
        if s.body:
            parts.append(s.body)
    return "\n\n".join(parts)


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
    """Split by headings, merge small sibling sections, then split oversized sections by
    paragraphs (~target / max tokens, ~15 % overlap between the pieces of one section)."""
    target_chars, max_chars = target_tokens * 4, max_tokens * 4
    overlap_chars = int(target_chars * overlap_ratio)
    drafts: list[ChunkDraft] = []

    def add(content: str, path: tuple[str, ...]) -> None:
        drafts.append(
            ChunkDraft(
                ordinal=len(drafts),
                content=content,
                heading_path=" > ".join(path) or None,
                token_count=estimate_tokens(content),
            )
        )

    for group in _group_sections(_parse_sections(text), target_chars, max_chars):
        if len(group) == 1:
            (section,) = group
            for piece in _pack(section.body, target_chars, max_chars, overlap_chars):
                add(piece, section.path)
            continue
        path = _common_path(group)
        content = _render_merged(group, path)
        if content.strip() and any(s.body for s in group):
            add(content, path)
    return drafts
