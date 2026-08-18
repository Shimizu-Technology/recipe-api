# Production Extraction Failure: Migration 015 Runbook

## Incident summary

Production extraction can fail before extraction starts with a `500 Internal Server Error` on:

```text
POST /api/extract/async
```

Observed production error:

```text
sqlalchemy.exc.ProgrammingError: asyncpg.exceptions.UndefinedColumnError: column extraction_jobs.user_id does not exist
```

Failing query:

```sql
SELECT extraction_jobs.id, extraction_jobs.url, extraction_jobs.user_id, ...
FROM extraction_jobs
WHERE (extraction_jobs.url = $1 OR extraction_jobs.url = $2)
  AND extraction_jobs.user_id = $3
ORDER BY extraction_jobs.created_at DESC
LIMIT $4
```

## Root cause

The deployed API code expects the database schema introduced by:

```text
migrations/015_add_extraction_job_user_id.py
```

But the production database has not run that migration yet.

This is not an Instagram-specific extraction failure. The Instagram URL in the logs simply reached the async extraction path. Any new extraction request can fail as soon as the API touches `ExtractionJob.user_id`.

## Why this migration exists

Migration `015` makes extraction jobs user-owned. This prevents:

- one user's extraction job from blocking another user's same URL;
- global URL uniqueness conflicts;
- job polling/cancellation without ownership checks.

It does three things:

1. Adds nullable `extraction_jobs.user_id`.
2. Backfills completed job ownership from linked `recipes.user_id`.
3. Drops the old global unique constraint on `extraction_jobs.url` and adds ownership indexes.

## Immediate production fix

Run migration `015` against the production database from the Render service environment or another confirmed production environment.

From Render Shell in `~/project/src`, use:

```bash
PYTHONPATH=. python migrations/015_add_extraction_job_user_id.py
```

The `PYTHONPATH=.` prefix is required because the migration imports the local `app` package from the project root.

If running locally against a confirmed non-production or explicitly-confirmed production `DATABASE_URL`, you can also use:

```bash
uv run python migrations/015_add_extraction_job_user_id.py
```

If using Render Shell or a Render one-off job, run it from the deployed service directory so it uses Render's production `DATABASE_URL`.

Do **not** run this from a local shell unless you have explicitly confirmed the local process is pointed at the production database.

## Expected successful output

```text
✓ Added user_id column to extraction_jobs
✓ Backfilled extraction job user_id from completed recipes
✓ Dropped global unique constraint on extraction_jobs.url if present
✓ Added extraction job ownership indexes
```

If it has already been applied, expected output includes:

```text
✓ extraction_jobs.user_id already exists
```

The migration is intended to be idempotent.

## Verification

After running the migration:

1. Confirm API health:

```bash
curl https://<api-host>/health
```

2. Test a signed-in extraction from the mobile app.
3. Check Render logs for:

```text
POST /api/extract/async HTTP/1.1" 200 OK
```

4. Confirm no new `UndefinedColumnError` for `extraction_jobs.user_id` appears.

## Optional SQL verification

If you are in a safe production SQL console:

```sql
SELECT column_name, data_type, is_nullable
FROM information_schema.columns
WHERE table_name = 'extraction_jobs'
  AND column_name = 'user_id';
```

Expected row:

```text
user_id | character varying | YES
```

Indexes should include:

```sql
SELECT indexname
FROM pg_indexes
WHERE tablename = 'extraction_jobs'
  AND indexname IN (
    'ix_extraction_jobs_user_id',
    'ix_extraction_jobs_url',
    'ix_extraction_jobs_user_url_status'
  );
```

## Rollback notes

A rollback should normally not be needed because this migration is forward-compatible with current code and uses a nullable column.

If a rollback were ever required, it would need to be deliberate because dropping `extraction_jobs.user_id` would break the current deployed API code.

## Prevention

Before deploying API code that depends on a migration:

- run the migration in production first if the old code can tolerate it; or
- deploy during a controlled maintenance window; and
- verify production schema before testing app flows.

For this repo, migrations are currently manual numbered scripts rather than Alembic-managed migrations. That means schema drift must be handled operationally.
