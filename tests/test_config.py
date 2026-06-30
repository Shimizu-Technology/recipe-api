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
