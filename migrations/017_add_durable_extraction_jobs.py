"""Migration 017: add the durable extraction queue state machine."""

import asyncio

from sqlalchemy import text

from app.db.database import engine


async def run_migration():
    """Add leasing, retry, expiry, idempotency, and persisted request fields."""
    async with engine.begin() as conn:
        await conn.execute(text("""
            ALTER TABLE extraction_jobs
                ADD COLUMN IF NOT EXISTS job_kind VARCHAR(16) NOT NULL DEFAULT 'extract',
                ADD COLUMN IF NOT EXISTS requested_is_public BOOLEAN NOT NULL DEFAULT FALSE,
                ADD COLUMN IF NOT EXISTS requested_display_name VARCHAR(100) NOT NULL DEFAULT 'A chef',
                ADD COLUMN IF NOT EXISTS target_recipe_id UUID REFERENCES recipes(id) ON DELETE SET NULL,
                ADD COLUMN IF NOT EXISTS idempotency_key VARCHAR(128),
                ADD COLUMN IF NOT EXISTS error_code VARCHAR(64),
                ADD COLUMN IF NOT EXISTS lease_token VARCHAR(64),
                ADD COLUMN IF NOT EXISTS leased_until TIMESTAMPTZ,
                ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMPTZ,
                ADD COLUMN IF NOT EXISTS attempt_count INTEGER NOT NULL DEFAULT 0,
                ADD COLUMN IF NOT EXISTS max_attempts INTEGER NOT NULL DEFAULT 3,
                ADD COLUMN IF NOT EXISTS next_attempt_at TIMESTAMPTZ,
                ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ
        """))
        await conn.execute(text("""
            ALTER TABLE extraction_jobs ALTER COLUMN status SET DEFAULT 'queued'
        """))

        await conn.execute(text("""
            UPDATE extraction_jobs
            SET job_kind = 'reextract',
                target_recipe_id = SUBSTRING(url FROM 're-extract:([0-9a-fA-F-]{36})')::uuid
            WHERE url ~ '^re-extract:[0-9a-fA-F-]{36}$'
        """))
        await conn.execute(text("""
            UPDATE extraction_jobs AS job
            SET url = recipe.source_url,
                requested_is_public = recipe.is_public,
                requested_display_name = COALESCE(recipe.extractor_display_name, 'A chef')
            FROM recipes AS recipe
            WHERE job.job_kind = 'reextract'
              AND job.target_recipe_id = recipe.id
        """))

        # Jobs that were tied to an old web process become recoverable queue work.
        await conn.execute(text("""
            UPDATE extraction_jobs
            SET status = 'queued',
                current_step = 'queued',
                message = 'Queued for extraction',
                lease_token = NULL,
                leased_until = NULL,
                next_attempt_at = NOW(),
                expires_at = COALESCE(expires_at, NOW() + INTERVAL '24 hours')
            WHERE status IN ('processing', 'pending', 'claimed')
              AND recipe_id IS NULL
        """))

        # Keep one active job per user/source before adding the partial invariant.
        await conn.execute(text("""
            WITH ranked AS (
                SELECT id,
                       ROW_NUMBER() OVER (
                           PARTITION BY user_id, job_kind, url, target_recipe_id
                           ORDER BY created_at DESC, id DESC
                       ) AS row_number
                FROM extraction_jobs
                WHERE status IN ('queued', 'claimed', 'processing')
            )
            UPDATE extraction_jobs AS job
            SET status = 'cancelled',
                current_step = 'cancelled',
                message = 'Superseded during durable queue migration',
                completed_at = NOW()
            FROM ranked
            WHERE job.id = ranked.id AND ranked.row_number > 1
        """))

        await conn.execute(text("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_extraction_jobs_active_user_url
            ON extraction_jobs (
                user_id,
                job_kind,
                url,
                COALESCE(target_recipe_id, '00000000-0000-0000-0000-000000000000'::uuid)
            )
            WHERE status IN ('queued', 'claimed', 'processing')
        """))
        await conn.execute(text("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_extraction_jobs_user_idempotency
            ON extraction_jobs (user_id, idempotency_key)
            WHERE idempotency_key IS NOT NULL
        """))
        await conn.execute(text("""
            CREATE INDEX IF NOT EXISTS ix_extraction_jobs_claimable
            ON extraction_jobs (status, next_attempt_at, created_at)
        """))
        await conn.execute(text("""
            CREATE INDEX IF NOT EXISTS ix_extraction_jobs_lease
            ON extraction_jobs (status, leased_until)
        """))
        print("✓ Added durable extraction queue fields and invariants")


if __name__ == "__main__":
    asyncio.run(run_migration())
