"""Public response/request models. Whitelisted fields only: never the raw tenant config."""

from pydantic import BaseModel

from core.tenants.config import Lang, TenantConfig


class WidgetConfig(BaseModel):
    name: str
    languages: list[Lang]
    default_language: Lang
    greeting: dict[Lang, str]
    starter_questions: dict[Lang, list[str]]
    launcher_label: dict[Lang, str]
    primary_color: str
    position: str
    privacy_notice_url: dict[Lang, str]
    privacy_notice_version: str
    max_message_chars: int


def widget_config(cfg: TenantConfig) -> WidgetConfig:
    return WidgetConfig(
        name=cfg.name,
        languages=cfg.languages,
        default_language=cfg.default_language,
        greeting=cfg.assistant.greeting,
        starter_questions=cfg.assistant.starter_questions,
        launcher_label=cfg.branding.launcher_label,
        primary_color=cfg.branding.primary_color,
        position=cfg.branding.position,
        privacy_notice_url=cfg.handoff.privacy_notice_url,
        privacy_notice_version=cfg.handoff.privacy_notice_version,
        max_message_chars=cfg.limits.max_message_chars,
    )
