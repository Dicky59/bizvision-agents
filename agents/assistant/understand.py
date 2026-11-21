"""The merged classify + rewrite_query step (ADR-005): one cheap LLM call."""

import json
import logging
import re
from typing import Any

from agents.assistant.context import history_text, neutralize_tags
from agents.assistant.prompts_loader import render
from agents.assistant.types import AssistantRequest, Intent, Understanding
from core.ingest.language import detect_language
from core.llm.client import LLMClient
from core.llm.types import Message
from core.tenants.config import Lang

log = logging.getLogger(__name__)

INTENTS: tuple[Intent, ...] = ("question", "contact_request", "smalltalk", "off_topic")
MAX_TOKENS = 300
_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


def _fallback_language(req: AssistantRequest) -> Lang:
    lang = req.language_hint or detect_language(req.message) or req.cfg.default_language
    return "fi" if lang == "fi" else "en"


def parse_understanding(text: str, req: AssistantRequest) -> Understanding:
    """Tolerant parser. Anything unusable degrades to a plain question with the raw message as
    query (retrieval still works, just without term expansion)."""
    data: Any = None
    m = _OBJECT.search(text)
    if m:
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            data = None
    if not isinstance(data, dict):
        return Understanding(
            intent="question",
            language=_fallback_language(req),
            other_language=False,
            wants_human=False,
            search_query=req.message,
            parse_failed=True,
        )

    intent: Intent = data["intent"] if data.get("intent") in INTENTS else "question"
    raw_lang = data.get("language")
    other = raw_lang == "other"
    language: Lang
    if raw_lang in ("fi", "en"):
        language = raw_lang
    elif other:
        language = "en"
    else:
        language = _fallback_language(req)
    query = data.get("search_query")
    if intent == "question":
        query = query.strip() if isinstance(query, str) and query.strip() else req.message
    else:
        query = ""
    return Understanding(
        intent=intent,
        language=language,
        other_language=other,
        wants_human=data.get("wants_human") is True,
        search_query=query,
    )


async def understand(llm: LLMClient, req: AssistantRequest) -> Understanding:
    cfg = req.cfg
    user = (
        f"<history>\n{history_text(req.history, cfg.assistant.history_turns)}\n</history>\n"
        f"<message>\n{neutralize_tags(req.message)}\n</message>"
    )
    result = await llm.complete(
        tenant_id=req.tenant_id,
        monthly_budget_eur=cfg.limits.monthly_budget_eur,
        operation="understand",
        messages=[Message("user", user)],
        system=render("understand", company=cfg.name),
        model=cfg.assistant.model,
        max_tokens=MAX_TOKENS,
        conversation_id=req.conversation_id,
    )
    u = parse_understanding(result.text, req)
    if u.parse_failed:
        log.warning("understand: unparsable output, falling back to raw query")
    return u
