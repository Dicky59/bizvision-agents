"""Domain exceptions for ingestion (mapped to HTTP errors in api/ later)."""


class IngestError(Exception):
    pass


class UnsafeUrl(IngestError):
    """URL rejected by the SSRF guard (scheme, host or resolved address not allowed)."""


class ResponseTooLarge(IngestError):
    pass


class TooManyRedirects(IngestError):
    pass


class FetchError(IngestError):
    pass


class FileTooLarge(IngestError):
    pass


class UnsupportedFile(IngestError):
    pass
