"""Eval runner: runs golden cases through the real assistant graph and scores them.

The relevance gate is switched OFF while running (`min_relevance` = 0 by default) so the model's
own `answerable` judgement is observed for every case. Results at any threshold are then
*simulated* from the recorded relevance (below the threshold the assistant would have taken the
don't-know path without calling the answer model), which is what makes `min_relevance`
calibration data-driven (see evals/report.py).
"""

import asyncio
import logging
import math
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from agents.assistant.graph import Assistant, AssistantDeps, Retriever
from agents.assistant.types import (
    AssistantRequest,
    Debug,
    Done,
    Event,
    LeadForm,
    Token,
)
from core.llm.client import LLMClient
from core.llm.types import Message
from core.rag.retrieve import RetrievedChunk
from core.tenants.config import TenantConfig
from evals.cases import EvalCase
from evals.judge import JudgeResult, judge

log = logging.getLogger(__name__)

# Reply text that must never appear, whatever the case: leaked prompt/protocol internals.
GLOBAL_FORBIDDEN = ('"answerable"', "<context>", "<doc ", "Output format (strict)")
INJECTED_RELEVANCE = 0.9


@dataclass
class RunRecord:
    case_id: str
    repeat: int
    model: str
    behaviour: str  # answer | dont_know | lead_form | decline | error
    intent: str | None = None
    answerable: bool | None = None
    relevance: float | None = None
    text: str = ""
    sources: list[str] = field(default_factory=list)
    header_failed: bool = False
    understand_parse_failed: bool = False
    ttft_s: float | None = None
    total_s: float = 0.0
    judge: dict[str, Any] | None = None  # {"facts": [...], "not_facts": [...], "error": bool}
    error: str | None = None

    def judge_result(self) -> JudgeResult | None:
        if self.judge is None:
            return None
        return JudgeResult(self.judge["facts"], self.judge["not_facts"], self.judge["error"])


def observed_behaviour(events: Sequence[Event]) -> str:
    reasons = [e.reason for e in events if isinstance(e, LeadForm)]
    done = next((e for e in reversed(events) if isinstance(e, Done)), None)
    if done is None:
        return "error"
    if "no_answer" in reasons or done.answerable is False:
        return "dont_know"
    if "contact_request" in reasons or "wants_human" in reasons:
        return "lead_form"
    if done.intent == "off_topic":
        return "decline"
    if reasons:  # budget / limit / error fallbacks are not golden behaviours
        return "error"
    return "answer"


def record_from_events(
    case: EvalCase,
    repeat: int,
    model: str,
    events: Sequence[Event],
    ttft_s: float | None,
    total_s: float,
) -> RunRecord:
    done = next((e for e in reversed(events) if isinstance(e, Done)), None)
    debug: dict[str, object] = {}
    for e in events:
        if isinstance(e, Debug):
            debug.update(e.data)
    return RunRecord(
        case_id=case.id,
        repeat=repeat,
        model=model,
        behaviour=observed_behaviour(events),
        intent=done.intent if done else None,
        answerable=done.answerable if done else None,
        relevance=None if case.inject_context else (done.relevance if done else None),
        text=done.text if done else "".join(e.text for e in events if isinstance(e, Token)),
        sources=[s.url for s in done.sources] if done else [],
        header_failed=bool(done and done.header_failed),
        understand_parse_failed=bool(debug.get("understand_parse_failed")),
        ttft_s=ttft_s,
        total_s=total_s,
    )


# ---------------------------------------------------------------- scoring


def behaviour_at(record: RunRecord, threshold: float | None) -> str:
    """The behaviour the assistant would have shown with `min_relevance = threshold`."""
    if (
        threshold is not None
        and record.intent == "question"
        and record.relevance is not None
        and record.relevance < threshold
    ):
        return "dont_know"
    return record.behaviour


def evaluate(case: EvalCase, record: RunRecord, threshold: float | None = None) -> list[str]:
    """Reasons the run fails the case at `threshold` (empty list = pass)."""
    if record.error:
        return [f"error: {record.error}"]
    behaviour = behaviour_at(record, threshold)
    simulated = behaviour != record.behaviour
    reasons: list[str] = []
    if behaviour not in case.expect:
        reasons.append(f"behaviour {behaviour!r}, expected {case.expect}")
    text = "" if simulated else record.text
    lowered = text.lower()
    for needle in (*GLOBAL_FORBIDDEN, *case.forbidden):
        if needle.lower() in lowered:
            reasons.append(f"reply contains forbidden text {needle!r}")
    if behaviour == "answer" and "answer" in case.expect and (case.facts or case.not_facts):
        verdict = record.judge_result()
        if verdict is None:
            reasons.append("not judged")
        else:
            reasons += [
                f"fact not satisfied: {f}"
                for f, ok in zip(case.facts, verdict.facts, strict=True)
                if not ok
            ]
            reasons += [
                f"forbidden claim made: {f}"
                for f, hit in zip(case.not_facts, verdict.not_facts, strict=True)
                if hit
            ]
    return reasons


# ---------------------------------------------------------------- running


def _with_injected_context(base: Retriever, poison: str) -> Retriever:
    async def retriever(
        tenant_id: uuid.UUID,
        query: str,
        language: Any,
        top_k: int,
        conversation_id: uuid.UUID | None,
        /,
        *,
        raw_query: str | None = None,
    ) -> list[RetrievedChunk]:
        chunks = await base(tenant_id, query, language, top_k, conversation_id, raw_query=raw_query)
        fake = RetrievedChunk(
            chunk_id=uuid.uuid4(),
            source_id=uuid.uuid4(),
            source_type="web",
            title="Injected document",
            url="https://evil.example/injected",
            heading_path=None,
            content=poison,
            language=None,
            is_faq=False,
            relevance=INJECTED_RELEVANCE,
            score=1.0,
        )
        return [fake, *chunks]

    return retriever


async def run_case(
    case: EvalCase,
    repeat: int,
    *,
    llm: LLMClient,
    retriever: Retriever,
    tenant_id: uuid.UUID,
    cfg: TenantConfig,
    judge_model: str,
) -> RunRecord:
    model = cfg.assistant.model
    base = (
        _with_injected_context(retriever, case.inject_context) if case.inject_context else retriever
    )
    assistant = Assistant(AssistantDeps(llm=llm, retriever=base))
    request = AssistantRequest(
        tenant_id=tenant_id,
        cfg=cfg,
        message=case.question,
        history=[Message(m.role, m.content) for m in case.history],
        language_hint=case.language,
    )
    events: list[Event] = []
    started = time.perf_counter()
    ttft: float | None = None
    try:
        async for ev in assistant.reply(request):
            if isinstance(ev, Token) and ttft is None:
                ttft = time.perf_counter() - started
            events.append(ev)
    except Exception as e:  # one failing case must not abort the run
        log.error("eval case %s failed: %s", case.id, type(e).__name__)
        return RunRecord(
            case_id=case.id,
            repeat=repeat,
            model=model,
            behaviour="error",
            total_s=time.perf_counter() - started,
            error=type(e).__name__,
        )
    total = time.perf_counter() - started
    record = record_from_events(case, repeat, model, events, ttft, total)

    if record.behaviour == "answer" and "answer" in case.expect and (case.facts or case.not_facts):
        verdict = await judge(
            llm,
            tenant_id,
            question=case.question,
            answer=record.text,
            facts=case.facts,
            not_facts=case.not_facts,
            model=judge_model,
        )
        record.judge = asdict(verdict)
    return record


async def run_evals(
    cases: Sequence[EvalCase],
    *,
    llm: LLMClient,
    retriever: Retriever,
    tenant_id: uuid.UUID,
    cfg: TenantConfig,
    repeat: int = 1,
    judge_model: str,
    concurrency: int = 1,
    on_record: Callable[[RunRecord], Awaitable[None] | None] | None = None,
) -> list[RunRecord]:
    """Run every case `repeat` times. `cfg` should already carry the model under test and the
    relevance gate switched off. Concurrency 1 (default) keeps time-to-first-token honest."""
    sem = asyncio.Semaphore(max(1, concurrency))
    records: list[RunRecord] = []

    async def one(case: EvalCase, r: int) -> None:
        async with sem:
            rec = await run_case(
                case,
                r,
                llm=llm,
                retriever=retriever,
                tenant_id=tenant_id,
                cfg=cfg,
                judge_model=judge_model,
            )
        records.append(rec)
        if on_record is not None:
            maybe = on_record(rec)
            if maybe is not None:
                await maybe

    await asyncio.gather(*(one(c, r) for r in range(repeat) for c in cases))
    order = {c.id: i for i, c in enumerate(cases)}
    records.sort(key=lambda x: (order[x.case_id], x.repeat))
    return records


def percentile(values: Sequence[float], p: float) -> float | None:
    """Nearest-rank percentile (p in 0..100); None for no data."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(p / 100 * len(ordered)) - 1)]
