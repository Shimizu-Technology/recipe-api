"""Migration 017: add the durable extraction queue state machine."""

import asyncio

from sqlalchemy import text

from app.db.database import engine


async def run_migration():
    """Add leasing, retry, expiry, idempotency, and persisted request fields."""
    async with engine.begin() as conn:
        durable_column_result = await conn.execute(text("""
            SELECT EXISTS (
                SELECT 1
                FROM information_schema.columns
                WHERE table_name = 'extraction_jobs'
                  AND column_name = 'lease_token'
            )
        """))
        is_legacy_upgrade = not durable_column_result.scalar_one()

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

        if is_legacy_upgrade:
            await conn.execute(text("""
                UPDATE extraction_jobs
                SET job_kind = 'reextract'
                WHERE url ~ '^re-extract:[0-9a-fA-F-]{36}$'
            """))
            # Historical re-extractions can outlive a recipe that was deleted.
            # Join through recipes before assigning the new foreign key so those
            # orphaned audit rows remain valid with a NULL target.
            await conn.execute(text("""
                UPDATE extraction_jobs AS job
                SET target_recipe_id = recipe.id
                FROM recipes AS recipe
                WHERE job.job_kind = 'reextract'
                  AND job.url ~ '^re-extract:[0-9a-fA-F-]{36}$'
                  AND SUBSTRING(
                      job.url FROM 're-extract:([0-9a-fA-F-]{36})'
                  )::uuid = recipe.id
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

            # A linked recipe proves the legacy process saved successfully.
            await conn.execute(text("""
                UPDATE extraction_jobs
                SET status = 'completed',
                    progress = 100,
                    current_step = 'complete',
                    message = 'Recipe extracted successfully!',
                    completed_at = COALESCE(completed_at, NOW())
                WHERE status IN ('queued', 'processing', 'pending', 'claimed')
                  AND recipe_id IS NOT NULL
            """))

            # An active re-extraction whose target recipe was deleted cannot be
            # recovered. Preserve the row and ask the client to start fresh.
            await conn.execute(text("""
                UPDATE extraction_jobs
                SET status = 'failed',
                    current_step = 'error',
                    message = 'The original recipe no longer exists; start a new extraction',
                    error_message = 'The original recipe no longer exists; start a new extraction',
                    error_code = 'MIGRATION_TARGET_MISSING',
                    completed_at = NOW(),
                    next_attempt_at = NULL
                WHERE status IN ('queued', 'processing', 'pending', 'claimed')
                  AND recipe_id IS NULL
                  AND job_kind = 'reextract'
                  AND target_recipe_id IS NULL
            """))

            # Re-extraction payloads can be reconstructed from the target recipe.
            await conn.execute(text("""
                UPDATE extraction_jobs
                SET status = 'queued',
                    current_step = 'queued',
                    message = 'Queued for re-extraction',
                    lease_token = NULL,
                    leased_until = NULL,
                    next_attempt_at = NOW(),
                    expires_at = COALESCE(expires_at, NOW() + INTERVAL '24 hours')
                WHERE status IN ('queued', 'processing', 'pending', 'claimed')
                  AND recipe_id IS NULL
                  AND job_kind = 'reextract'
                  AND target_recipe_id IS NOT NULL
            """))

            # Regular legacy jobs did not persist visibility/display-name inputs.
            # Failing them explicitly is safer than creating a recipe with guessed
            # privacy or attribution; the client can submit a new durable request.
            await conn.execute(text("""
                UPDATE extraction_jobs
                SET status = 'failed',
                    current_step = 'error',
                    message = 'Please retry extraction after the queue upgrade',
                    error_message = 'Please retry extraction after the queue upgrade',
                    error_code = 'MIGRATION_RETRY_REQUIRED',
                    completed_at = NOW(),
                    next_attempt_at = NULL
                WHERE status IN ('queued', 'processing', 'pending', 'claimed')
                  AND recipe_id IS NULL
                  AND job_kind = 'extract'
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
