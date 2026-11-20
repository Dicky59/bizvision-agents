"""Tiny fi/en language detector (stopword ratio); no dependency."""

import re

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


def detect_language(text: str, hint: str | None = None) -> str | None:
    """Return 'fi' / 'en'; the (valid) hint wins, otherwise a stopword heuristic, else None."""
    hinted = normalize_lang_tag(hint)
    if hinted:
        return hinted
    words = [w.lower() for w in _WORD.findall(text[:5000])]
    if len(words) < 8:
        return None
    fi = sum(w in _FI for w in words)
    en = sum(w in _EN for w in words)
    # ä/ö are strong Finnish signals
    fi += sum(1 for w in words if "ä" in w or "ö" in w) // 2
    if fi == en or max(fi, en) < 2:
        return None
    return "fi" if fi > en else "en"
