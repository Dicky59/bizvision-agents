"""LLM-as-judge for fact checks (cheap model; usage is recorded as operation `eval_judge`)."""

import json
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agents.assistant.context import neutralize_tags
from core.llm.client import LLMClient
from core.llm.types import Message

PROMPT = Path(__file__).parent / "prompts" / "judge.md"
DEFAULT_JUDGE_MODEL = "claude-haiku-5-5"
_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


@dataclass(frozen=True)
class JudgeResult:
    facts: list[bool]  # satisfied, per fact
    not_facts: list[bool]  # violated, per not-fact
    error: bool = False


def parse_judge(text: str, n_facts: int, n_not_facts: int) -> JudgeResult:
    """Strict on shape: a malformed verdict fails every fact (never passes by accident)."""
    failed = JudgeResult([False] * n_facts, [True] * n_not_facts, error=True)
    m = _OBJECT.search(text)
    if not m:
        return failed
    try:
        data: Any = json.loads(m.group(0))
    except json.JSONDecodeError:
        return failed
    facts, nots = (data.get("facts"), data.get("not_facts")) if isinstance(data, dict) else (0, 0)
    ok = (
        isinstance(facts, list)
        and isinstance(nots, list)
        and len(facts) == n_facts
        and len(nots) == n_not_facts
        and all(isinstance(x, bool) for x in (*facts, *nots))
    )
    return JudgeResult(list(facts), list(nots)) if ok else failed  # type: ignore[arg-type]


async def judge(
    llm: LLMClient,
    tenant_id: uuid.UUID,
    *,
    question: str,
    answer: str,
    facts: list[str],
    not_facts: list[str],
    model: str = DEFAULT_JUDGE_MODEL,
    monthly_budget_eur: float | None = None,
) -> JudgeResult:
    listing = "\n".join(
        [
            "FACTS:",
            *[f"{i}. {f}" for i, f in enumerate(facts, 1)],
            "NOT_FACTS:",
            *[f"{i}. {f}" for i, f in enumerate(not_facts, 1)],
        ]
    )
    user = (
        f"<question>\n{neutralize_tags(question)}\n</question>\n"
        f"<answer>\n{neutralize_tags(answer)}\n</answer>\n\n{listing}"
    )
    result = await llm.complete(
        tenant_id=tenant_id,
        monthly_budget_eur=monthly_budget_eur,
        operation="eval_judge",
        messages=[Message("user", user)],
        system=PROMPT.read_text(encoding="utf-8"),
        model=model,
        max_tokens=200,
    )
    return parse_judge(result.text, len(facts), len(not_facts))
