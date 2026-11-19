from pathlib import Path

import yaml

from core.tenants.config import TenantConfig


def load_yaml(path: Path) -> TenantConfig:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a YAML mapping at top level")
    return TenantConfig.model_validate(data)
