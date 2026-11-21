"""Golden eval cases: schema and loader."""

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from core.tenants.config import Lang

Behaviour = Literal["answer", "dont_know", "lead_form", "decline"]
Category = Literal["answer", "dont_know", "lead_form", "decline", "injection"]


class HistoryMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["user", "assistant"]
    content: str


class EvalCase(BaseModel):
    """One golden case.

    `expect` is the set of acceptable behaviours (a single string is accepted). For answers,
    `facts` are statements the answer must satisfy and `not_facts` statements it must not; both
    are judged semantically by a cheap LLM. `forbidden` are strings that must not appear in the
    reply (case-insensitive), e.g. an injected canary. `inject_context` adds a poisoned document
    to the retrieved context for this case only (context-borne prompt injection).
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    category: Category
    question: str = Field(min_length=1)
    language: Lang | None = None
    history: list[HistoryMessage] = []
    expect: list[Behaviour] = Field(min_length=1)
    facts: list[str] = []
    not_facts: list[str] = []
    forbidden: list[str] = []
    inject_context: str | None = None
    note: str = ""

    @field_validator("expect", mode="before")
    @classmethod
    def _single(cls, v: object) -> object:
        return [v] if isinstance(v, str) else v

    @model_validator(mode="after")
    def _consistent(self) -> "EvalCase":
        if (self.facts or self.not_facts) and "answer" not in self.expect:
            raise ValueError(
                f"{self.id}: facts/not_facts only make sense when 'answer' is expected"
            )
        if self.category != "injection" and self.category not in self.expect:
            raise ValueError(f"{self.id}: category {self.category!r} must be in expect")
        return self


class EvalSet(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cases: list[EvalCase]

    @model_validator(mode="after")
    def _unique_ids(self) -> "EvalSet":
        ids = [c.id for c in self.cases]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"duplicate case ids: {dupes}")
        return self


def load_cases(path: Path) -> list[EvalCase]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return EvalSet.model_validate(data).cases
