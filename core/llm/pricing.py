"""Hand-maintained price table (USD per million tokens). Unknown model -> cost 0 + warning.

Sources: Anthropic model/pricing docs as of 2026-11-03; OpenAI pricing for the embedding model.
Haiku 5.5 prices apply to prompts up to 100K tokens (higher beyond) - our prompts are far below.
Cache rates for claude-haiku-5-5 are assumed (read 0.1x, write 1.25x input); update when verified.
"""

import logging
from dataclasses import dataclass
from decimal import Decimal

from core.llm.types import Usage

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModelInfo:
    input_usd: float
    output_usd: float
    cache_read_usd: float | None = None  # default 0.1x input
    cache_write_usd: float | None = None  # default 1.25x input
    # Chat-model request quirk (see ADR-003)
    thinking_off: dict[str, str] | None = None  # `thinking` value that disables/limits thinking

    @property
    def cache_read(self) -> float:
        return self.cache_read_usd if self.cache_read_usd is not None else self.input_usd * 0.1

    @property
    def cache_write(self) -> float:
        return self.cache_write_usd if self.cache_write_usd is not None else self.input_usd * 1.25


MODELS: dict[str, ModelInfo] = {
    "claude-haiku-5-5": ModelInfo(0.10, 0.50, thinking_off={"type": "disabled"}),
    "claude-sonnet-5-5": ModelInfo(
        2.00,
        10.00,
        cache_read_usd=0.10,
        thinking_off={"type": "between_tools"},
    ),
    "claude-opus-5-5": ModelInfo(4.00, 20.00, cache_read_usd=0.20),
    "text-embedding-3-small": ModelInfo(0.02, 0.0),
}


def get_model_info(model: str) -> ModelInfo | None:
    return MODELS.get(model)


def cost_eur(model: str, usage: Usage, eur_per_usd: float) -> Decimal:
    info = MODELS.get(model)
    if info is None:
        log.warning("no pricing for model %s; recording cost 0", model)
        return Decimal(0)
    usd = (
        usage.input_tokens * info.input_usd
        + usage.output_tokens * info.output_usd
        + usage.cache_read_tokens * info.cache_read
        + usage.cache_write_tokens * info.cache_write
    ) / 1_000_000
    return Decimal(str(round(usd * eur_per_usd, 10)))
