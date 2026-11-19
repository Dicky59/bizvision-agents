import logging
from collections.abc import AsyncIterator, Sequence
from typing import Any

import anthropic
import httpx2

from core.llm.pricing import get_model_info
from core.llm.types import LLMEvent, LLMResult, Message, TextDelta, Usage, UsageUpdate
from core.retry import retry_async

log = logging.getLogger(__name__)

CONNECTION_ERRORS = (anthropic.APIConnectionError, anthropic.APITimeoutError)


class AnthropicProvider:
    name = "anthropic"

    def __init__(
        self,
        api_key: str,
        *,
        timeout_s: float = 60.0,
        http_client: httpx2.AsyncClient | None = None,
        sleep: Any = None,
    ) -> None:
        # max_retries=0: retry policy is ours (core.retry) so 529 / retry-after are handled.
        self._client = anthropic.AsyncAnthropic(
            api_key=api_key, timeout=timeout_s, max_retries=0, http_client=http_client
        )
        self._retry_kwargs: dict[str, Any] = {"sleep": sleep} if sleep else {}

    def _request(
        self,
        messages: Sequence[Message],
        system: str,
        model: str,
        max_tokens: int,
        temperature: float | None,
        tools: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        info = get_model_info(model)
        params: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            # Static system prompt is the cacheable prefix. Below the model's minimum
            # cacheable length this silently does nothing.
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        }
        # `temperature` is accepted for interface stability but never sent: sampling parameters
        # are rejected by current models and absent from the SDK (see ADR-003).
        del temperature
        if info is not None and info.thinking_off is not None:
            params["thinking"] = info.thinking_off
        if tools:
            params["tools"] = tools
        return params

    async def complete(
        self,
        *,
        messages: Sequence[Message],
        system: str,
        model: str,
        max_tokens: int,
        temperature: float | None,
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResult:
        params = self._request(messages, system, model, max_tokens, temperature, tools)
        resp = await retry_async(
            lambda: self._client.messages.create(**params),
            connection_errors=CONNECTION_ERRORS,
            **self._retry_kwargs,
        )
        text = "".join(b.text for b in resp.content if b.type == "text")
        tool_calls = [
            {"id": b.id, "name": b.name, "input": b.input}
            for b in resp.content
            if b.type == "tool_use"
        ]
        return LLMResult(
            text=text,
            usage=_usage(resp.usage, resp.usage.output_tokens),
            model=resp.model,
            stop_reason=resp.stop_reason,
            tool_calls=tool_calls,
        )

    async def stream(
        self,
        *,
        messages: Sequence[Message],
        system: str,
        model: str,
        max_tokens: int,
        temperature: float | None,
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[LLMEvent]:
        params = self._request(messages, system, model, max_tokens, temperature, tools)
        # Only establishing the stream is retried; once events flow, errors propagate.
        events = await retry_async(
            lambda: self._client.messages.create(stream=True, **params),
            connection_errors=CONNECTION_ERRORS,
            **self._retry_kwargs,
        )
        try:
            usage = Usage()
            async for ev in events:
                if ev.type == "message_start":
                    usage = _usage(ev.message.usage, 0)
                    yield UsageUpdate(usage=usage, final=False)
                elif ev.type == "content_block_delta" and ev.delta.type == "text_delta":
                    yield TextDelta(ev.delta.text)
                elif ev.type == "message_delta":
                    usage = Usage(
                        input_tokens=usage.input_tokens,
                        output_tokens=ev.usage.output_tokens,
                        cache_read_tokens=usage.cache_read_tokens,
                        cache_write_tokens=usage.cache_write_tokens,
                    )
                    yield UsageUpdate(usage=usage, final=True, stop_reason=ev.delta.stop_reason)
        finally:
            await events.close()


def _usage(u: Any, output_tokens: int) -> Usage:
    return Usage(
        input_tokens=u.input_tokens or 0,
        output_tokens=output_tokens,
        cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
        cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
    )
