from fastapi import APIRouter
from fastapi.responses import FileResponse

from api.deps import Deps, PublicTenant
from api.errors import ApiError
from api.schemas import WidgetConfig, widget_config

router = APIRouter(prefix="/v1/widget", tags=["widget"])
script_router = APIRouter(tags=["widget"])


@router.get("/{slug}/config")
async def get_config(slug: str, tenant: PublicTenant) -> WidgetConfig:
    return widget_config(tenant.config)


@script_router.get("/widget.v1.js", include_in_schema=False)
async def widget_script(deps: Deps) -> FileResponse:
    """The built widget. Public, tenant-independent; a script tag needs no CORS."""
    path = deps.widget_dist / "widget.js"
    if not path.is_file():
        raise ApiError(404, "widget_not_built")
    return FileResponse(
        path,
        media_type="application/javascript",
        headers={"Cache-Control": "public, max-age=86400"},
    )
