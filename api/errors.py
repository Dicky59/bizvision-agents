"""HTTP error mapping. Bodies are `{"error": {"code": ...}}`; nothing request-derived is echoed."""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


class ApiError(Exception):
    def __init__(self, status: int, code: str, *, headers: dict[str, str] | None = None) -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.headers = headers or {}


def forbidden() -> ApiError:
    """One answer for unknown tenant, paused tenant and disallowed origin (no enumeration)."""
    return ApiError(403, "forbidden")


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            {"error": {"code": exc.code}}, status_code=exc.status, headers=exc.headers
        )
