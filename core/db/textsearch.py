"""Postgres text-search configuration per language (shared by ingestion and retrieval).

The config name is chosen from a fixed whitelist and passed as a bound parameter cast to
regconfig, never interpolated into SQL.
"""

from sqlalchemy import String, cast, literal
from sqlalchemy.dialects.postgresql import REGCONFIG
from sqlalchemy.sql.elements import ColumnElement

_CONFIGS = {"fi": "finnish", "en": "english"}


def ts_config(language: str | None) -> str:
    """'fi' -> finnish, 'en' -> english, anything else -> simple."""
    return _CONFIGS.get(language or "", "simple")


def ts_config_expr(language: str | None) -> ColumnElement:
    return cast(literal(ts_config(language), String), REGCONFIG)
