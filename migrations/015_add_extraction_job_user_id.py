"""
Migration 015: Add user_id to extraction_jobs and remove global URL uniqueness.

Extraction jobs are user-owned. The old global unique constraint on url prevented
multiple users from extracting the same URL and allowed job IDs to be polled or
cancelled without ownership checks.
"""

import asyncio

from sqlalchemy import text

from app.db.database import engine


async def run_migration():
    """Add user_id and replace the global URL unique constraint with indexes."""
    async with engine.begin() as conn:
        result = await conn.execute(text("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = 'extraction_jobs' AND column_name = 'user_id'
        """))

        if not result.fetchone():
            await conn.execute(text("""
                ALTER TABLE extraction_jobs
                ADD COLUMN user_id VARCHAR(64)
            """))
            print("✓ Added user_id column to extraction_jobs")
        else:
            print("✓ extraction_jobs.user_id already exists")

        await conn.execute(text("""
            UPDATE extraction_jobs AS job
            SET user_id = recipe.user_id
            FROM recipes AS recipe
            WHERE job.recipe_id = recipe.id
              AND job.user_id IS NULL
              AND recipe.user_id IS NOT NULL
        """))
        print("✓ Backfilled extraction job user_id from completed recipes")

        await conn.execute(text("""
            ALTER TABLE extraction_jobs
            DROP CONSTRAINT IF EXISTS extraction_jobs_url_key
        """))
        print("✓ Dropped global unique constraint on extraction_jobs.url if present")

        await conn.execute(text("""
            CREATE INDEX IF NOT EXISTS ix_extraction_jobs_user_id
            ON extraction_jobs (user_id)
        """))
        await conn.execute(text("""
            CREATE INDEX IF NOT EXISTS ix_extraction_jobs_url
            ON extraction_jobs (url)
        """))
        await conn.execute(text("""
            CREATE INDEX IF NOT EXISTS ix_extraction_jobs_user_url_status
            ON extraction_jobs (user_id, url, status)
        """))
        print("✓ Added extraction job ownership indexes")


if __name__ == "__main__":
    asyncio.run(run_migration())
