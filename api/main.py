import logging

from fastapi import FastAPI

from api.cors import TenantCORSMiddleware, make_origin_check
from api.deps import ApiDeps
from api.errors import install_error_handlers
from api.routers import admin, assistant, leads, widget
from core.config import get_settings


def create_app(deps: ApiDeps | None = None) -> FastAPI:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)
    docs = settings.enable_docs
    app = FastAPI(
        title="bizvision-agents",
        docs_url="/docs" if docs else None,
        redoc_url="/redoc" if docs else None,
        openapi_url="/openapi.json" if docs else None,
    )
    app.state.deps = deps or ApiDeps()
    install_error_handlers(app)
    app.add_middleware(
        TenantCORSMiddleware,
        origin_check=make_origin_check(lambda: app.state.deps.sessionmaker()),
    )
    app.include_router(widget.router)
    app.include_router(widget.script_router)
    app.include_router(assistant.router)
    app.include_router(leads.router)
    app.include_router(admin.router)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
