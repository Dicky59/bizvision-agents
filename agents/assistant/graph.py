"""The assistant's LangGraph pipeline (spec §7.1, as amended by ADR-005).

    understand -> question        -> retrieve -> relevance gate -> answer -> finish
                                                    | below min          | answerable=false
                                                    v                    v
                                                 dont_know ----------> finish
               -> contact_request -> offer_contact -> finish
               -> smalltalk       -> smalltalk (LLM) -> finish
               -> off_topic       -> decline (static) -> finish

Nodes push visitor-facing events through LangGraph's custom stream writer; `Assistant.reply`
yields them. Input guards and persistence are layered around `reply` (milestone steps 4-5).
"""

import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import Any

from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph

from agents.assistant.answer import HeaderGate
from agents.assistant.context import build_context, extract_sources, recent_history
from agents.assistant.messages import static_message
from agents.assistant.prompts_loader import render
from agents.assistant.types import (
    AssistantRequest,
    Debug,
    Done,
    Event,
    LeadForm,
    SourceRef,
    Sources,
    Token,
    Understanding,
)
from agents.assistant.understand import understand
from core.llm.client import LLMClient
from core.llm.types import Message
from core.rag.retrieve import RetrievedChunk, best_relevance
from core.tenants.config import Lang

log = logging.getLogger(__name__)

# (tenant_id, query, language, top_k, conversation_id) -> chunks
Retriever = Callable[[uuid.UUID, str, Lang, int, uuid.UUID | None], Awaitable[list[RetrievedChunk]]]

SMALLTALK_MAX_TOKENS = 200
_LANGUAGE_RULE: dict[str, str] = {
    "fi": "Answer in Finnish.",
    "en": "Answer in English.",
    "other": (
        "The visitor writes in a language you do not support. Answer in English and politely "
        "say that you can only help in Finnish and English."
    ),
}


@dataclass(frozen=True)
class AssistantDeps:
    llm: LLMClient
    retriever: Retriever


@dataclass
class GraphState:
    request: AssistantRequest
    understanding: Understanding | None = None
    chunks: list[RetrievedChunk] = field(default_factory=list)
    relevance: float | None = None
    answer_text: str = ""
    sources: list[SourceRef] = field(default_factory=list)
    answerable: bool | None = None
    header_failed: bool = False
    lead_form_sent: bool = False


def _u(state: GraphState) -> Understanding:
    if state.understanding is None:
        raise RuntimeError("understanding missing: node ran before understand")
    return state.understanding


def _language_rule(u: Understanding) -> str:
    return _LANGUAGE_RULE["other" if u.other_language else u.language]


class Assistant:
    def __init__(self, deps: AssistantDeps) -> None:
        self._deps = deps
        self._graph = self._build()

    async def reply(self, request: AssistantRequest) -> AsyncIterator[Event]:
        stream = self._graph.astream({"request": request}, stream_mode="custom")
        async with aclosing(stream):
            async for event in stream:
                yield event

    # ------------------------------------------------------------------ nodes

    def _build(self) -> Any:
        deps = self._deps

        async def understand_node(state: GraphState) -> dict[str, Any]:
            req = state.request
            u = await understand(deps.llm, req)
            get_stream_writer()(
                Debug(
                    {
                        "intent": u.intent,
                        "language": u.language,
                        "wants_human": u.wants_human,
                        "search_query": u.search_query,
                        "understand_parse_failed": u.parse_failed,
                    }
                )
            )
            return {"understanding": u}

        def route_intent(state: GraphState) -> str:
            return {
                "question": "retrieve",
                "contact_request": "offer_contact",
                "smalltalk": "smalltalk",
                "off_topic": "decline",
            }[_u(state).intent]

        async def retrieve_node(state: GraphState) -> dict[str, Any]:
            req, u = state.request, _u(state)
            chunks = await deps.retriever(
                req.tenant_id,
                u.search_query,
                u.language,
                req.cfg.assistant.retrieval.top_k,
                req.conversation_id,
            )
            relevance = best_relevance(chunks)
            get_stream_writer()(
                Debug(
                    {
                        "relevance": round(relevance, 4),
                        "min_relevance": req.cfg.assistant.retrieval.min_relevance,
                        "retrieved": [c.url or c.title or "-" for c in chunks],
                    }
                )
            )
            return {"chunks": chunks, "relevance": relevance}

        def route_relevance(state: GraphState) -> str:
            min_rel = state.request.cfg.assistant.retrieval.min_relevance
            if state.chunks and (state.relevance or 0.0) >= min_rel:
                return "answer"
            return "dont_know"

        async def answer_node(state: GraphState) -> dict[str, Any]:
            req, u, chunks = state.request, _u(state), state.chunks
            cfg = req.cfg
            writer = get_stream_writer()
            context, docs = build_context(chunks)
            system = render(
                "answer",
                company=cfg.name,
                company_description=cfg.assistant.company_description,
                tone=cfg.assistant.tone,
                language_rule=_language_rule(u),
            )
            messages = [
                *recent_history(req.history, cfg.assistant.history_turns),
                Message("user", f"{context}\n\n<question>\n{req.message}\n</question>"),
            ]
            gate = HeaderGate()
            parts: list[str] = []
            stream = deps.llm.stream(
                tenant_id=req.tenant_id,
                monthly_budget_eur=cfg.limits.monthly_budget_eur,
                operation="answer",
                messages=messages,
                system=system,
                model=cfg.assistant.model,
                max_tokens=cfg.assistant.max_output_tokens,
                conversation_id=req.conversation_id,
            )
            # aclosing: on `break` the stream is closed right away, so the model stops and usage
            # is recorded through LLMClient's early-close path (estimated=true).
            async with aclosing(stream):
                async for delta in stream:
                    out = gate.feed(delta.text)
                    if out:
                        parts.append(out)
                        writer(Token(out))
                    if gate.rejected:
                        break
                else:
                    out = gate.finish()
                    if out:
                        parts.append(out)
                        writer(Token(out))

            writer(Debug({"answerable": not gate.rejected, "header_failed": gate.header_failed}))
            if gate.rejected:
                return {"answerable": False, "header_failed": gate.header_failed}
            text = "".join(parts)
            sources = extract_sources(text, docs)
            if sources:
                writer(Sources(sources))
            return {
                "answer_text": text,
                "sources": sources,
                "answerable": True,
                "header_failed": gate.header_failed,
            }

        def route_answer(state: GraphState) -> str:
            return "finish" if state.answerable else "dont_know"

        async def dont_know_node(state: GraphState) -> dict[str, Any]:
            req, u = state.request, _u(state)
            text = static_message(req.cfg, "dont_know", u.language)
            writer = get_stream_writer()
            writer(Token(text))
            writer(LeadForm("no_answer"))
            return {"answer_text": text, "sources": [], "lead_form_sent": True}

        async def offer_contact_node(state: GraphState) -> dict[str, Any]:
            req, u = state.request, _u(state)
            text = static_message(req.cfg, "contact_offer", u.language)
            writer = get_stream_writer()
            writer(Token(text))
            writer(LeadForm("contact_request"))
            return {"answer_text": text, "sources": [], "lead_form_sent": True}

        async def decline_node(state: GraphState) -> dict[str, Any]:
            req, u = state.request, _u(state)
            text = static_message(req.cfg, "decline", u.language)
            get_stream_writer()(Token(text))
            return {"answer_text": text, "sources": []}

        async def smalltalk_node(state: GraphState) -> dict[str, Any]:
            req, u = state.request, _u(state)
            cfg = req.cfg
            writer = get_stream_writer()
            topics = "; ".join(cfg.assistant.starter_questions.get(u.language, [])[:3])
            system = render(
                "smalltalk",
                company=cfg.name,
                company_description=cfg.assistant.company_description,
                tone=cfg.assistant.tone,
                language_rule=_language_rule(u),
                topics=topics or cfg.name,
            )
            messages = [
                *recent_history(req.history, cfg.assistant.history_turns),
                Message("user", f"<message>\n{req.message}\n</message>"),
            ]
            parts: list[str] = []
            stream = deps.llm.stream(
                tenant_id=req.tenant_id,
                monthly_budget_eur=cfg.limits.monthly_budget_eur,
                operation="smalltalk",
                messages=messages,
                system=system,
                model=cfg.assistant.model,
                max_tokens=SMALLTALK_MAX_TOKENS,
                conversation_id=req.conversation_id,
            )
            async with aclosing(stream):
                async for delta in stream:
                    parts.append(delta.text)
                    writer(Token(delta.text))
            return {"answer_text": "".join(parts), "sources": []}

        async def finish_node(state: GraphState) -> dict[str, Any]:
            u = _u(state)
            writer = get_stream_writer()
            sent = state.lead_form_sent
            if u.wants_human and not sent:
                writer(LeadForm("wants_human"))
                sent = True
            writer(
                Done(
                    text=state.answer_text,
                    sources=state.sources,
                    intent=u.intent,
                    language=u.language,
                    answerable=state.answerable,
                    relevance=state.relevance,
                    header_failed=state.header_failed,
                    lead_form=sent,
                )
            )
            return {"lead_form_sent": sent}

        g = StateGraph(GraphState)
        g.add_node("understand", understand_node)
        g.add_node("retrieve", retrieve_node)
        g.add_node("answer", answer_node)
        g.add_node("dont_know", dont_know_node)
        g.add_node("offer_contact", offer_contact_node)
        g.add_node("smalltalk", smalltalk_node)
        g.add_node("decline", decline_node)
        g.add_node("finish", finish_node)

        g.add_edge(START, "understand")
        g.add_conditional_edges(
            "understand",
            route_intent,
            ["retrieve", "offer_contact", "smalltalk", "decline"],
        )
        g.add_conditional_edges("retrieve", route_relevance, ["answer", "dont_know"])
        g.add_conditional_edges("answer", route_answer, ["finish", "dont_know"])
        for node in ("dont_know", "offer_contact", "smalltalk", "decline"):
            g.add_edge(node, "finish")
        g.add_edge("finish", END)
        return g.compile()
