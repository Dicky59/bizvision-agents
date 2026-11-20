"""Tiny fi/en language detector (stopword ratio); no dependency."""

import logging
import re

log = logging.getLogger(__name__)

_FI = frozenset(
    "ja on ei että se oli mutta kun tai myös kanssa sekä ovat tämä voit meille meidän "
    "yritys palvelu palvelut hinta miten mitä miksi sinä sinun olla ole jos niin kuin "
    "vain jo tai sen sitä siitä joka jotka kaikki yhteyttä ota".split()
)
_EN = frozenset(
    "the and is are was were but when or also with for that this you your we our of to in "
    "it as at by from can will have has how what why not if so than then which all contact".split()
)
_WORD = re.compile(r"[a-zA-ZäöåÄÖÅ]+")


def normalize_lang_tag(tag: str | None) -> str | None:
    """'fi-FI' -> 'fi'; only languages we support are returned."""
    if not tag:
        return None
    primary = tag.strip().lower().replace("_", "-").split("-")[0]
    return primary if primary in ("fi", "en") else None


def _score(text: str) -> tuple[str | None, int, int, int]:
    """Return (winner, winner_hits, loser_hits, word_count) of the stopword heuristic."""
    words = [w.lower() for w in _WORD.findall(text[:5000])]
    fi = sum(w in _FI for w in words)
    en = sum(w in _EN for w in words)
    # ä/ö are strong Finnish signals
    fi += sum(1 for w in words if "ä" in w or "ö" in w) // 2
    if len(words) < 8 or fi == en or max(fi, en) < 2:
        return None, 0, 0, len(words)
    return ("fi", fi, en, len(words)) if fi > en else ("en", en, fi, len(words))


def _confident(winner_hits: int, loser_hits: int, words: int) -> bool:
    """Confident enough to overrule a declared language: enough text, clear margin."""
    return words >= 20 and winner_hits >= 5 and winner_hits >= 3 * loser_hits


def detect_language(text: str, hint: str | None = None) -> str | None:
    """Return 'fi' / 'en'.

    Without a usable hint the stopword heuristic decides. With a hint (e.g. `<html lang>`) the
    hint wins unless the text is confidently the other language: sites do mislabel pages, and the
    language drives full-text config and retrieval boosting, so the text is the better witness.
    """
    hinted = normalize_lang_tag(hint)
    winner, hits, other, words = _score(text)
    if hinted:
        if winner and winner != hinted and _confident(hits, other, words):
            log.debug("language hint %r overruled by text detection %r", hinted, winner)
            return winner
        return hinted
    return winner
