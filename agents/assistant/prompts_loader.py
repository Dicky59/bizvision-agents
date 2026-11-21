"""Prompt templates live in `prompts/*.md` (architecture rule 4).

Placeholders are `{lower_snake}` only; every other brace (e.g. JSON examples) is left alone.
Substitution is single-pass, so values are never re-scanned for placeholders.
"""

import re
from functools import cache
from pathlib import Path

PROMPTS_DIR = Path(__file__).parent / "prompts"
_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")


@cache
def _template(name: str) -> str:
    return (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")


def render(name: str, **values: str) -> str:
    def sub(m: re.Match[str]) -> str:
        key = m.group(1)
        if key not in values:
            raise KeyError(f"prompt '{name}' needs a value for {{{key}}}")
        return values[key]

    return _PLACEHOLDER.sub(sub, _template(name))
