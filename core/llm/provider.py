from collections.abc import AsyncIterator, Sequence
from typing import Any, Protocol

from core.llm.types import LLMEvent, LLMResult, Message


class LLMProvider(Protocol):
    name: str

    async def complete(
        self,
        *,
        messages: Sequence[Message],
        system: str,
        model: str,
        max_tokens: int,
        temperature: float | None,
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResult: ...

    def stream(
        self,
        *,
        messages: Sequence[Message],
        system: str,
        model: str,
        max_tokens: int,
        temperature: float | None,
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[LLMEvent]: ...
