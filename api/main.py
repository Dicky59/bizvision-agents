import logging

from fastapi import FastAPI

from api.cors import TenantCORSMiddleware, make_origin_check
from api.deps import ApiDeps
from api.errors import install_error_handlers
from api.routers import assistant, widget
from core.config import get_settings


def create_app(deps: ApiDeps | None = None) -> FastAPI:
    logging.basicConfig(level=get_settings().log_level)
    app = FastAPI(title="bizvision-agents")
    app.state.deps = deps or ApiDeps()
    install_error_handlers(app)
    app.add_middleware(
        TenantCORSMiddleware,
        origin_check=make_origin_check(lambda: app.state.deps.sessionmaker()),
    )
    app.include_router(widget.router)
    app.include_router(assistant.router)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
