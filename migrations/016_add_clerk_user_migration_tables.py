"""
Migration 016: Add Clerk user migration mapping tables.

These tables support the Clerk development -> production cutover. We store a
hashed email mapping to the old Clerk development user ID, then record when that
legacy user has been migrated to a new Clerk production user ID.
"""

import asyncio

from sqlalchemy import text

from app.db.database import engine


async def run_migration():
    """Create Clerk migration mapping/tracking tables."""
    async with engine.begin() as conn:
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS legacy_clerk_user_mappings (
                legacy_user_id VARCHAR(64) PRIMARY KEY,
                email_hash VARCHAR(128) NOT NULL UNIQUE,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            )
        """))
        print("✓ Ensured legacy_clerk_user_mappings table exists")

        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS clerk_user_migrations (
                legacy_user_id VARCHAR(64) PRIMARY KEY REFERENCES legacy_clerk_user_mappings(legacy_user_id),
                new_user_id VARCHAR(64) NOT NULL UNIQUE,
                email_hash VARCHAR(128) NOT NULL,
                migrated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            )
        """))
        print("✓ Ensured clerk_user_migrations table exists")

        await conn.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_legacy_clerk_user_mappings_email_hash
            ON legacy_clerk_user_mappings (email_hash)
        """))
        await conn.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_clerk_user_migrations_new_user_id
            ON clerk_user_migrations (new_user_id)
        """))
        await conn.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_clerk_user_migrations_email_hash
            ON clerk_user_migrations (email_hash)
        """))
        print("✓ Added Clerk migration indexes")


if __name__ == "__main__":
    asyncio.run(run_migration())
