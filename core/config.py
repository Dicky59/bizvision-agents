from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://bv:bv@localhost:5432/bv"
    database_url_direct: str = "postgresql+asyncpg://bv:bv@localhost:5432/bv"
    test_database_url: str = "postgresql+asyncpg://bv:bv@localhost:5432/bv_test"
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    resend_api_key: str = ""
    email_from: str = "BizVision AI <ai@bizvision.fi>"
    admin_api_key: str = ""
    public_base_url: str = "http://localhost:8000"
    log_level: str = "INFO"
    eur_per_usd: float = 0.92


@lru_cache
def get_settings() -> Settings:
    return Settings()
