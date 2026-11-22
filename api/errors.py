"""HTTP error mapping. Bodies are `{"error": {"code": ...}}`; nothing request-derived is echoed."""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


class ApiError(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        *,
        headers: dict[str, str] | None = None,
        detail: str | None = None,  # admin endpoints only; never request-derived visitor data
    ) -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.headers = headers or {}
        self.detail = detail


def forbidden() -> ApiError:
    """One answer for unknown tenant, paused tenant and disallowed origin (no enumeration)."""
    return ApiError(403, "forbidden")


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        error: dict[str, str] = {"code": exc.code}
        if exc.detail:
            error["detail"] = exc.detail
        return JSONResponse({"error": error}, status_code=exc.status, headers=exc.headers)
