from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(frozen=True)
class Message:
    role: Literal["user", "assistant"]
    content: str


@dataclass(frozen=True)
class Usage:
    """Token counts. input_tokens excludes cache read/write tokens (Anthropic semantics)."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass(frozen=True)
class LLMResult:
    text: str
    usage: Usage
    model: str
    stop_reason: str | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class UsageUpdate:
    """Usage so far. final=True only once the provider reported the end-of-stream totals."""

    usage: Usage
    final: bool
    stop_reason: str | None = None


LLMEvent = TextDelta | UsageUpdate
