import importlib

from app.config import get_settings


def _reload_user_migration(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@example.com/db")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setenv("CLERK_MIGRATION_EMAIL_HASH_SECRET", "test-secret")
    get_settings.cache_clear()

    import app.services.user_migration as user_migration

    return importlib.reload(user_migration)


def test_normalize_email_trims_and_lowercases(monkeypatch):
    user_migration = _reload_user_migration(monkeypatch)

    assert user_migration.normalize_email(" Person@Example.COM ") == "person@example.com"


def test_hash_email_is_stable_for_normalized_email(monkeypatch):
    user_migration = _reload_user_migration(monkeypatch)

    assert user_migration.hash_email("Person@Example.COM", "secret") == user_migration.hash_email(
        " person@example.com ",
        "secret",
    )


def test_hash_email_changes_with_secret(monkeypatch):
    user_migration = _reload_user_migration(monkeypatch)

    assert user_migration.hash_email("person@example.com", "secret-1") != user_migration.hash_email(
        "person@example.com",
        "secret-2",
    )


def test_migration_email_hash_returns_none_when_disabled(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@example.com/db")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.delenv("CLERK_MIGRATION_EMAIL_HASH_SECRET", raising=False)
    get_settings.cache_clear()

    import app.services.user_migration as user_migration

    user_migration = importlib.reload(user_migration)

    assert user_migration.migration_email_hash("person@example.com") is None


def test_merge_recipe_note_text_preserves_distinct_notes(monkeypatch):
    user_migration = _reload_user_migration(monkeypatch)

    assert user_migration.merge_recipe_note_text("Production note", "Legacy note") == (
        "Production note\n\n---\n\nLegacy note"
    )


def test_merge_recipe_note_text_avoids_empty_or_duplicate_content(monkeypatch):
    user_migration = _reload_user_migration(monkeypatch)

    assert user_migration.merge_recipe_note_text("Production note", "   ") == "Production note"
    assert user_migration.merge_recipe_note_text("  ", "Legacy note") == "Legacy note"
    assert user_migration.merge_recipe_note_text("Same note", "Same note") == "Same note"
