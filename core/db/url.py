from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def normalize_db_url(url: str) -> str:
    """Make a libpq-style URL (e.g. from Neon) acceptable to asyncpg.

    ``sslmode=require`` becomes ``ssl=require``; ``channel_binding`` is dropped
    because asyncpg rejects it. URLs without these params are returned unchanged.
    """
    parts = urlsplit(url)
    if not parts.query:
        return url
    params: list[tuple[str, str]] = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key == "channel_binding":
            continue
        if key == "sslmode":
            key = "ssl"
        params.append((key, value))
    return urlunsplit(parts._replace(query=urlencode(params)))
