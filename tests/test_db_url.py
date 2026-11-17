from core.db.url import normalize_db_url


def test_local_url_unchanged() -> None:
    url = "postgresql+asyncpg://bv:bv@localhost:5432/bv"
    assert normalize_db_url(url) == url


def test_neon_url_converted() -> None:
    url = (
        "postgresql+asyncpg://user:pw@ep-cool-123.eu-central-1.aws.neon.tech/neondb"
        "?sslmode=require&channel_binding=require"
    )
    assert normalize_db_url(url) == (
        "postgresql+asyncpg://user:pw@ep-cool-123.eu-central-1.aws.neon.tech/neondb?ssl=require"
    )


def test_other_params_preserved() -> None:
    url = "postgresql+asyncpg://u:p@h/db?sslmode=require&application_name=bv"
    assert normalize_db_url(url) == "postgresql+asyncpg://u:p@h/db?ssl=require&application_name=bv"


def test_ssl_param_already_asyncpg_style_unchanged() -> None:
    url = "postgresql+asyncpg://u:p@h/db?ssl=require"
    assert normalize_db_url(url) == url
