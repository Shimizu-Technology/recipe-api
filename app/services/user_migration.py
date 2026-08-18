"""Legacy Clerk user migration helpers.

This module supports the Clerk development -> production cutover. It maps a
verified email hash to the old Clerk development user ID, then rewrites user-owned
rows to the new Clerk production user ID on first production sign-in.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings

settings = get_settings()


@dataclass(frozen=True)
class LegacyMigrationResult:
    """Result of attempting a legacy Clerk user migration."""

    status: str
    migrated: bool
    legacy_user_id: str | None = None
    new_user_id: str | None = None
    rows_updated: dict[str, int] | None = None
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "migrated": self.migrated,
            "legacy_user_id": self.legacy_user_id,
            "new_user_id": self.new_user_id,
            "rows_updated": self.rows_updated or {},
            "reason": self.reason,
        }


def normalize_email(email: str) -> str:
    """Normalize an email for migration matching."""
    return email.strip().lower()


def hash_email(email: str, secret: str) -> str:
    """Return an HMAC-SHA256 email hash for the migration mapping table."""
    normalized = normalize_email(email)
    return hmac.new(
        secret.encode("utf-8"),
        normalized.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def migration_email_hash(email: str) -> str | None:
    """Hash an email with the configured migration secret, if enabled."""
    secret = settings.clerk_migration_email_hash_secret
    if not secret:
        return None
    return hash_email(email, secret)


def merge_recipe_note_text(current_text: str, legacy_text: str) -> str:
    """Preserve distinct note content when two Clerk identities collide."""
    if not legacy_text.strip():
        return current_text
    if not current_text.strip():
        return legacy_text
    if current_text == legacy_text:
        return current_text
    return f"{current_text}\n\n---\n\n{legacy_text}"


async def migrate_legacy_user_data(
    db: AsyncSession,
    *,
    new_user_id: str,
    email: str | None,
) -> LegacyMigrationResult:
    """Migrate legacy Clerk-owned rows to a production Clerk user ID.

    This function is intentionally idempotent. It no-ops when migration is not
    configured, when the signed-in user has no email, when no legacy mapping
    exists, or when the migration has already been completed.
    """
    if not settings.clerk_migration_email_hash_secret:
        return LegacyMigrationResult(
            status="disabled",
            migrated=False,
            new_user_id=new_user_id,
            reason="CLERK_MIGRATION_EMAIL_HASH_SECRET is not configured",
        )

    if not email:
        return LegacyMigrationResult(
            status="missing_email",
            migrated=False,
            new_user_id=new_user_id,
            reason="No email claim was available for the signed-in Clerk user",
        )

    email_hash = migration_email_hash(email)
    if not email_hash:
        return LegacyMigrationResult(status="disabled", migrated=False, new_user_id=new_user_id)

    mapping_result = await db.execute(
        text("""
            SELECT legacy_user_id
            FROM legacy_clerk_user_mappings
            WHERE email_hash = :email_hash
        """),
        {"email_hash": email_hash},
    )
    legacy_user_id = mapping_result.scalar_one_or_none()

    if not legacy_user_id:
        return LegacyMigrationResult(
            status="no_mapping",
            migrated=False,
            new_user_id=new_user_id,
            reason="No legacy Clerk user mapping matched this email",
        )

    if legacy_user_id == new_user_id:
        return LegacyMigrationResult(
            status="same_user",
            migrated=False,
            legacy_user_id=legacy_user_id,
            new_user_id=new_user_id,
            reason="Signed-in user already matches the legacy user ID",
        )

    # Prevent concurrent requests for the same mapping from racing.
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
        {"lock_key": legacy_user_id},
    )

    existing_result = await db.execute(
        text("""
            SELECT legacy_user_id, new_user_id
            FROM clerk_user_migrations
            WHERE legacy_user_id = :legacy_user_id OR new_user_id = :new_user_id
            LIMIT 1
        """),
        {"legacy_user_id": legacy_user_id, "new_user_id": new_user_id},
    )
    existing = existing_result.mappings().first()

    if existing:
        if existing["legacy_user_id"] == legacy_user_id and existing["new_user_id"] == new_user_id:
            return LegacyMigrationResult(
                status="already_migrated",
                migrated=False,
                legacy_user_id=legacy_user_id,
                new_user_id=new_user_id,
            )

        return LegacyMigrationResult(
            status="conflict",
            migrated=False,
            legacy_user_id=legacy_user_id,
            new_user_id=new_user_id,
            reason="Legacy mapping has already been associated with a different user",
        )

    insert_result = await db.execute(
        text("""
            INSERT INTO clerk_user_migrations (legacy_user_id, new_user_id, email_hash)
            VALUES (:legacy_user_id, :new_user_id, :email_hash)
            ON CONFLICT DO NOTHING
            RETURNING legacy_user_id
        """),
        {
            "legacy_user_id": legacy_user_id,
            "new_user_id": new_user_id,
            "email_hash": email_hash,
        },
    )
    inserted_legacy_user_id = insert_result.scalar_one_or_none()
    if not inserted_legacy_user_id:
        return LegacyMigrationResult(
            status="conflict",
            migrated=False,
            legacy_user_id=legacy_user_id,
            new_user_id=new_user_id,
            reason="Migration record could not be inserted because another migration completed first",
        )

    rows_updated: dict[str, int] = {}

    # Remove rows that would violate unique constraints after the user_id update.
    rows_updated["saved_recipe_duplicates_deleted"] = (
        await db.execute(
            text("""
                DELETE FROM saved_recipes AS old
                USING saved_recipes AS new
                WHERE old.user_id = :legacy_user_id
                  AND new.user_id = :new_user_id
                  AND old.recipe_id = new.recipe_id
            """),
            {"legacy_user_id": legacy_user_id, "new_user_id": new_user_id},
        )
    ).rowcount or 0

    # A user may have written notes under both Clerk identities. Preserve both
    # values in the surviving production-identity row before removing the
    # duplicate that would violate UNIQUE(user_id, recipe_id).
    note_collisions = (
        await db.execute(
            text("""
                SELECT new.id, new.note_text AS current_text,
                       old.note_text AS legacy_text,
                       old.updated_at AS legacy_updated_at
                FROM recipe_notes AS old
                JOIN recipe_notes AS new ON new.recipe_id = old.recipe_id
                WHERE old.user_id = :legacy_user_id
                  AND new.user_id = :new_user_id
            """),
            {"legacy_user_id": legacy_user_id, "new_user_id": new_user_id},
        )
    ).mappings().all()

    for collision in note_collisions:
        await db.execute(
            text("""
                UPDATE recipe_notes
                SET note_text = :note_text,
                    updated_at = GREATEST(updated_at, :legacy_updated_at)
                WHERE id = :note_id
            """),
            {
                "note_id": collision["id"],
                "note_text": merge_recipe_note_text(
                    collision["current_text"], collision["legacy_text"]
                ),
                "legacy_updated_at": collision["legacy_updated_at"],
            },
        )
    rows_updated["recipe_note_duplicates_merged"] = len(note_collisions)

    rows_updated["recipe_note_duplicates_deleted"] = (
        await db.execute(
            text("""
                DELETE FROM recipe_notes AS old
                USING recipe_notes AS new
                WHERE old.user_id = :legacy_user_id
                  AND new.user_id = :new_user_id
                  AND old.recipe_id = new.recipe_id
            """),
            {"legacy_user_id": legacy_user_id, "new_user_id": new_user_id},
        )
    ).rowcount or 0

    rows_updated["grocery_member_duplicates_deleted"] = (
        await db.execute(
            text("""
                DELETE FROM grocery_list_members AS old
                USING grocery_list_members AS new
                WHERE old.user_id = :legacy_user_id
                  AND new.user_id = :new_user_id
                  AND old.list_id = new.list_id
            """),
            {"legacy_user_id": legacy_user_id, "new_user_id": new_user_id},
        )
    ).rowcount or 0

    update_statements = {
        "recipes": "UPDATE recipes SET user_id = :new_user_id WHERE user_id = :legacy_user_id",
        "saved_recipes": "UPDATE saved_recipes SET user_id = :new_user_id WHERE user_id = :legacy_user_id",
        "collections": "UPDATE collections SET user_id = :new_user_id WHERE user_id = :legacy_user_id",
        "recipe_notes": "UPDATE recipe_notes SET user_id = :new_user_id WHERE user_id = :legacy_user_id",
        "recipe_versions": "UPDATE recipe_versions SET created_by = :new_user_id WHERE created_by = :legacy_user_id",
        "meal_plan_entries": "UPDATE meal_plan_entries SET user_id = :new_user_id WHERE user_id = :legacy_user_id",
        "grocery_items": "UPDATE grocery_items SET user_id = :new_user_id WHERE user_id = :legacy_user_id",
        "grocery_list_members": "UPDATE grocery_list_members SET user_id = :new_user_id WHERE user_id = :legacy_user_id",
        "grocery_list_invites_created": "UPDATE grocery_list_invites SET created_by = :new_user_id WHERE created_by = :legacy_user_id",
        "grocery_list_invites_accepted": "UPDATE grocery_list_invites SET accepted_by = :new_user_id WHERE accepted_by = :legacy_user_id",
        "extraction_jobs": "UPDATE extraction_jobs SET user_id = :new_user_id WHERE user_id = :legacy_user_id",
    }

    params = {"legacy_user_id": legacy_user_id, "new_user_id": new_user_id}
    for key, statement in update_statements.items():
        result = await db.execute(text(statement), params)
        rows_updated[key] = result.rowcount or 0

    return LegacyMigrationResult(
        status="migrated",
        migrated=True,
        legacy_user_id=legacy_user_id,
        new_user_id=new_user_id,
        rows_updated=rows_updated,
    )
