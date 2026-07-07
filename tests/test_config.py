from app.config import Settings


def _settings(database_url: str) -> Settings:
    return Settings(
        database_url=database_url,
        openai_api_key="test-openai-key",
    )


def test_async_database_url_removes_sslmode_and_preserves_other_query_params():
    settings = _settings(
        "postgresql://user:pass@example.com/db?sslmode=require&application_name=hafa"
    )

    assert (
        settings.async_database_url
        == "postgresql+asyncpg://user:pass@example.com/db?application_name=hafa"
    )


def test_async_database_url_removes_sslmode_when_it_is_not_first_param():
    settings = _settings(
        "postgresql://user:pass@example.com/db?application_name=hafa&sslmode=require"
    )

    assert (
        settings.async_database_url
        == "postgresql+asyncpg://user:pass@example.com/db?application_name=hafa"
    )


def test_async_database_url_removes_channel_binding_for_asyncpg():
    settings = _settings(
        "postgresql://user:pass@example.com/db?sslmode=require&channel_binding=require"
    )

    assert settings.async_database_url == "postgresql+asyncpg://user:pass@example.com/db"


def test_async_database_url_removes_channel_binding_and_preserves_supported_params():
    settings = _settings(
        "postgresql://user:pass@example.com/db?channel_binding=require&application_name=hafa"
    )

    assert (
        settings.async_database_url
        == "postgresql+asyncpg://user:pass@example.com/db?application_name=hafa"
    )


def test_clerk_issuers_defaults_to_primary_issuer():
    settings = Settings(
        database_url="postgresql://user:pass@example.com/db",
        openai_api_key="test-openai-key",
        clerk_jwt_issuer="https://primary.clerk.accounts.dev/",
    )

    assert settings.clerk_issuers == ["https://primary.clerk.accounts.dev"]


def test_clerk_issuers_parses_cutover_allowlist():
    settings = Settings(
        database_url="postgresql://user:pass@example.com/db",
        openai_api_key="test-openai-key",
        clerk_jwt_issuers="https://old.clerk.accounts.dev/, https://new.clerk.accounts.dev",
    )

    assert settings.clerk_issuers == [
        "https://old.clerk.accounts.dev",
        "https://new.clerk.accounts.dev",
    ]


def test_jwks_url_for_issuer_strips_trailing_slash():
    settings = _settings("postgresql://user:pass@example.com/db")

    assert (
        settings.jwks_url_for_issuer("https://example.clerk.accounts.dev/")
        == "https://example.clerk.accounts.dev/.well-known/jwks.json"
    )
