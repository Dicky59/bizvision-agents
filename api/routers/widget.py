from fastapi import APIRouter

from api.deps import PublicTenant
from api.schemas import WidgetConfig, widget_config

router = APIRouter(prefix="/v1/widget", tags=["widget"])


@router.get("/{slug}/config")
async def get_config(slug: str, tenant: PublicTenant) -> WidgetConfig:
    return widget_config(tenant.config)
