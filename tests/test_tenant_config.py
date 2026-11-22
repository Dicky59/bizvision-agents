import copy
from pathlib import Path

import pytest
from pydantic import ValidationError

from core.tenants.config import TenantConfig
from core.tenants.loader import load_yaml
from core.tenants.service import origin_allowed

ROOT = Path(__file__).resolve().parent.parent
MIN = {
    "slug": "acme",
    "name": "Acme",
    "handoff": {"email": "a@acme.fi", "privacy_notice_version": "1"},
}


def test_bizvision_yaml_loads() -> None:
    cfg = load_yaml(ROOT / "tenants" / "bizvision.yaml")
    assert cfg.slug == "bizvision"
    assert cfg.assistant.model == "claude-haiku-5-5"
    assert "https://bizvision.fi" in cfg.allowed_origins


def test_minimal_config_gets_defaults() -> None:
    cfg = TenantConfig.model_validate(MIN)
    assert cfg.default_language == "fi" and cfg.retention_days == 90
    assert cfg.limits.monthly_budget_eur == 30


def test_display_name_is_optional_and_set_for_bizvision() -> None:
    assert TenantConfig.model_validate(MIN).branding.display_name is None
    cfg = load_yaml(ROOT / "tenants" / "bizvision.yaml")
    assert cfg.name == "BizVision Oy" and cfg.branding.display_name == "BizVision"


@pytest.mark.parametrize(
    "patch",
    [
        {"slug": "Bad Slug"},
        {"default_language": "en", "languages": ["fi"]},
        {"allowed_origins": ["bizvision.fi"]},
        {"allowed_origins": ["https://x.fi/path"]},
        {"unknown_field": 1},
        {"assistant": {"retrieval": {"min_relevance": 1.5}}},
        {"handoff": {"email": "not-an-email", "privacy_notice_version": "1"}},
        {"branding": {"primary_color": "red"}},
    ],
)
def test_invalid_configs_rejected(patch: dict) -> None:
    with pytest.raises(ValidationError):
        TenantConfig.model_validate({**copy.deepcopy(MIN), **patch})


def test_origin_matrix() -> None:
    cfg = TenantConfig.model_validate({**MIN, "allowed_origins": ["https://Acme.fi/"]})
    assert origin_allowed(cfg, "https://acme.fi")
    assert origin_allowed(cfg, "https://acme.fi/")
    assert not origin_allowed(cfg, "https://evil.fi")
    assert not origin_allowed(cfg, "http://acme.fi")
    assert not origin_allowed(cfg, None)
