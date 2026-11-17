import pytest

from core.config import Settings


def test_defaults_load_without_api_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "RESEND_API_KEY", "ADMIN_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.anthropic_api_key == ""
    assert settings.eur_per_usd == 0.92


def test_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("EUR_PER_USD", "0.5")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.log_level == "DEBUG"
    assert settings.eur_per_usd == 0.5
