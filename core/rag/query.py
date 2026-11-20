"""Turn a user question into a safe OR tsquery string.

`websearch_to_tsquery` ANDs every term, so a natural question such as "Paljonko AI-asiakaspalvelija
maksaa?" only matches chunks containing *all* its words and the full-text leg of hybrid search
almost never fires. We build an OR query instead and let `ts_rank_cd` prefer chunks matching more
terms. Only letters and digits survive tokenisation, so the result can never contain tsquery
operators (`& | ! : ( ) <->`) or quotes.
"""

import re

MAX_TERMS = 12
_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)  # letters and digits, no underscore


def build_or_query(text: str) -> str | None:
    """'Paljonko maksaa hinta?' -> 'paljonko | maksaa | hinta'; None if nothing usable."""
    terms: list[str] = []
    for token in _TOKEN.findall(text.lower()):
        if len(token) > 1 and token not in terms:
            terms.append(token)
        if len(terms) == MAX_TERMS:
            break
    return " | ".join(terms) if terms else None
