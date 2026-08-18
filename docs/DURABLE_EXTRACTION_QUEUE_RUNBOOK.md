# Durable Extraction Queue Runbook

## Purpose

Async extraction and re-extraction requests are persisted in PostgreSQL before
the API responds. This prevents a deploy, process restart, or transient provider
failure from silently losing a user's work.

## State model

| State | Meaning | Client behavior |
| --- | --- | --- |
| `queued` | Persisted and waiting, including retry backoff | Keep polling; show queued/retrying copy |
| `claimed` | One worker owns a renewable lease | Keep polling |
| `processing` | Extraction is running | Keep polling and show progress |
| `completed` | Recipe and job transition committed | Open `recipe_id` |
| `failed` | Non-retryable failure or attempts exhausted | Stop polling; show `error_message` |
| `cancelled` | User cancelled the job | Stop polling |
| `expired` | The configured job lifetime elapsed | Stop polling; offer a new extraction |

`completed`, `failed`, `cancelled`, and `expired` are terminal states.

## Safety invariants

- A request payload commits before its job ID is returned.
- One active extraction exists per user/source/type; one active re-extraction
  exists per user/recipe.
- `Idempotency-Key` is unique per user and may only be reused with the same
  payload.
- Workers claim rows with `FOR UPDATE SKIP LOCKED` and increment the attempt.
- A renewable lease identifies the live worker. Every write is fenced by its
  lease token, so a stale worker cannot overwrite a recovered attempt.
- Recipe creation and the durable `recipe_id` link commit together. Recovery
  completes a linked job instead of creating a duplicate recipe.
- Re-extraction, version history, and terminal job state commit together.
- Cancellation and completion lock the same job row and therefore serialize.
- Raw provider errors are not exposed to clients. Diagnostics contain bounded
  counts and configuration state only.

## Production rollout

1. Confirm the target service/database before opening a production shell.
2. From the production service environment, run:

   ```bash
   PYTHONPATH=. python migrations/017_add_durable_extraction_jobs.py
   ```

3. Run the same command once more. The migration is idempotent and the second
   run must also succeed.
4. Confirm the new columns and indexes:

   ```sql
   SELECT column_name
   FROM information_schema.columns
   WHERE table_name = 'extraction_jobs'
     AND column_name IN (
       'job_kind', 'idempotency_key', 'lease_token', 'leased_until',
       'attempt_count', 'next_attempt_at', 'expires_at'
     )
   ORDER BY column_name;

   SELECT indexname
   FROM pg_indexes
   WHERE tablename = 'extraction_jobs'
     AND indexname IN (
       'uq_extraction_jobs_active_user_url',
       'uq_extraction_jobs_user_idempotency',
       'ix_extraction_jobs_claimable',
       'ix_extraction_jobs_lease'
     )
   ORDER BY indexname;
   ```

5. Deploy the application with `JOB_WORKER_ENABLED=true`.
6. Verify `GET /up` returns `{"status":"ok"}`.
7. As an admin, verify `GET /api/admin/diagnostics` reports the database as
   connected and includes `job_queue` counts.
8. Submit one real extraction, background the mobile app, and confirm it reaches
   `completed` after resuming without creating a duplicate recipe.

Do not deploy the worker code before migration 017. Startup intentionally fails
when the worker is enabled but required columns are missing, leaving the prior
deployment serving traffic.

## Rollback

1. Set `JOB_WORKER_ENABLED=false` in the production service.
2. Redeploy/restart the API.
3. Leave migration 017 and job history in place. The migration is expand-only;
   removing columns or indexes is unnecessary and makes recovery harder.
4. Diagnose the worker issue, deploy a fix, and re-enable the worker. Queued and
   stale leased jobs remain recoverable.

Disabling the worker pauses queued work; it does not delete or fail jobs.

## Incident checks

### Queue appears stuck

```sql
SELECT status, COUNT(*)
FROM extraction_jobs
GROUP BY status
ORDER BY status;

SELECT id, job_kind, status, attempt_count, max_attempts,
       leased_until, heartbeat_at, next_attempt_at, expires_at
FROM extraction_jobs
WHERE status IN ('queued', 'claimed', 'processing')
ORDER BY created_at
LIMIT 100;
```

- `queued` with a future `next_attempt_at` is normal retry backoff.
- `claimed` or `processing` with an expired lease is automatically recoverable.
- Attempts at `max_attempts` become failed during periodic cleanup.
- Jobs past `expires_at` become expired during periodic cleanup.

### Duplicate submissions

Confirm the mobile client sends a fresh UUID `Idempotency-Key` for each user
action and reuses that same key only when retrying the same request. The database
partial unique indexes remain the final concurrency guard.

### Worker repeatedly restarts

Check deployment logs for the migration preflight error first. Then check
database connectivity and configuration validation. Do not disable TLS outside
local development; `DATABASE_USE_SSL=false` is rejected in non-development
environments.

## Operational metrics

The initial admin diagnostic exposes bounded counts by job state. Alerting and
historical latency/error-rate dashboards are part of the observability roadmap;
until then, use the state and oldest-active-job queries above during incidents.
