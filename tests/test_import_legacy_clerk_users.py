import importlib

import pytest

from app.config import get_settings


def _reload_import_script(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@example.com/db")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setenv("CLERK_MIGRATION_EMAIL_HASH_SECRET", "test-secret")
    get_settings.cache_clear()

    import scripts.import_legacy_clerk_users as import_script

    return importlib.reload(import_script)


@pytest.mark.asyncio
async def test_import_csv_dry_run_rejects_duplicate_legacy_user_id(monkeypatch, tmp_path):
    import_script = _reload_import_script(monkeypatch)
    csv_path = tmp_path / "legacy-users.csv"
    csv_path.write_text(
        "legacy_user_id,email\n"
        "user_1,one@example.com\n"
        "user_1,two@example.com\n"
    )

    with pytest.raises(ValueError, match="Duplicate legacy_user_id"):
        await import_script.import_csv(csv_path, dry_run=True)


@pytest.mark.asyncio
async def test_import_csv_dry_run_rejects_duplicate_email(monkeypatch, tmp_path):
    import_script = _reload_import_script(monkeypatch)
    csv_path = tmp_path / "legacy-users.csv"
    csv_path.write_text(
        "legacy_user_id,email\n"
        "user_1,person@example.com\n"
        "user_2, Person@Example.com \n"
    )

    with pytest.raises(ValueError, match="Duplicate email mapping"):
        await import_script.import_csv(csv_path, dry_run=True)


@pytest.mark.asyncio
async def test_import_csv_dry_run_accepts_unique_rows(monkeypatch, tmp_path):
    import_script = _reload_import_script(monkeypatch)
    csv_path = tmp_path / "legacy-users.csv"
    csv_path.write_text(
        "legacy_user_id,email\n"
        "user_1,one@example.com\n"
        "user_2,two@example.com\n"
    )

    assert await import_script.import_csv(csv_path, dry_run=True) == 2
