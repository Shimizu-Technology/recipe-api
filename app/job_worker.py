"""Database-backed extraction worker with leases, retries, and stale recovery."""

import asyncio
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from sqlalchemy import and_, func, or_, select, text

from app.config import get_settings
from app.db.database import AsyncSessionLocal
from app.models.recipe import ExtractionJob

settings = get_settings()
ACTIVE_JOB_STATUSES = frozenset({"queued", "claimed", "processing"})
TERMINAL_JOB_STATUSES = frozenset({"completed", "failed", "cancelled", "expired"})
RETRYABLE_EXTRACTION_ERROR_CODES = frozenset(
    {
        "IMAGE_DOWNLOAD_FAILED",
        "LLM_EXTRACTION_FAILED",
        "SYSTEM_ERROR",
        "TIMEOUT",
        "UNKNOWN_ERROR",
        "VISION_EXTRACTION_FAILED",
    }
)
REQUIRED_JOB_COLUMNS = frozenset(
    {
        "job_kind",
        "lease_token",
        "leased_until",
        "attempt_count",
        "expires_at",
        "idempotency_key",
    }
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def missing_worker_columns(existing_columns: set[str]) -> list[str]:
    return sorted(REQUIRED_JOB_COLUMNS - existing_columns)


def should_retry_extraction_error(error_code: str | None) -> bool:
    """Return whether a provider/media failure is likely transient."""
    return bool(error_code and error_code.upper() in RETRYABLE_EXTRACTION_ERROR_CODES)


def apply_claim(job: ExtractionJob, now: datetime) -> None:
    """Apply the claimed state and a fresh lease to an already-locked job."""
    job.status = "claimed"
    job.current_step = "claimed"
    job.message = "Preparing extraction..."
    job.lease_token = uuid4().hex
    job.leased_until = now + timedelta(seconds=settings.job_lease_seconds)
    job.heartbeat_at = now
    job.attempt_count = (job.attempt_count or 0) + 1
    job.next_attempt_at = None
    job.updated_at = now


def apply_retry_policy(job: ExtractionJob, now: datetime, error_code: str) -> None:
    """Apply a bounded retry or terminal failure to an already-locked job."""
    job.error_code = error_code[:64]
    job.lease_token = None
    job.leased_until = None
    job.heartbeat_at = now
    job.updated_at = now
    if job.attempt_count < job.max_attempts:
        delay_seconds = min(300, 2 ** max(0, job.attempt_count - 1) * 15)
        job.status = "queued"
        job.current_step = "retrying"
        job.message = "A temporary error occurred. Retrying extraction..."
        job.next_attempt_at = now + timedelta(seconds=delay_seconds)
    else:
        job.status = "failed"
        job.current_step = "error"
        job.message = "Extraction failed after multiple attempts"
        job.completed_at = now
        job.next_attempt_at = None


def apply_recovered_completion(job: ExtractionJob, now: datetime) -> None:
    """Finish a replayed job whose recipe link committed before worker exit."""
    job.status = "completed"
    job.progress = 100
    job.current_step = "complete"
    job.message = "Recipe extracted successfully!"
    job.completed_at = now
    job.next_attempt_at = None
    job.lease_token = None
    job.leased_until = None
    job.updated_at = now


def claimable_job_query(now: datetime):
    """Build the locking query shared by workers across API replicas."""
    return (
        select(ExtractionJob)
        .where(
            or_(
                and_(
                    ExtractionJob.status == "queued",
                    or_(
                        ExtractionJob.next_attempt_at.is_(None),
                        ExtractionJob.next_attempt_at <= now,
                    ),
                ),
                and_(
                    ExtractionJob.status.in_(("claimed", "processing")),
                    ExtractionJob.leased_until < now,
                ),
            ),
            or_(ExtractionJob.expires_at.is_(None), ExtractionJob.expires_at > now),
            ExtractionJob.attempt_count < ExtractionJob.max_attempts,
        )
        .order_by(ExtractionJob.created_at.asc())
        .with_for_update(skip_locked=True)
        .limit(1)
    )


class DurableJobWorker:
    """Claim and execute persisted jobs safely across deploys and API replicas."""

    def __init__(self):
        self._task: asyncio.Task | None = None
        self._wake_event = asyncio.Event()
        self._last_cleanup_at: datetime | None = None

    async def start(self) -> None:
        if not settings.job_worker_enabled or (self._task and not self._task.done()):
            return
        await self.verify_schema()
        self._task = asyncio.create_task(self._run(), name="durable-extraction-worker")

    async def verify_schema(self) -> None:
        """Fail deployment before serving if migration 017 was not applied."""
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                text("""
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_name = 'extraction_jobs'
                """)
            )
            existing_columns = set(result.scalars().all())
        missing = missing_worker_columns(existing_columns)
        if missing:
            raise RuntimeError(
                "Migration 017 is required before enabling the durable job worker; "
                f"missing columns: {', '.join(missing)}"
            )

    async def stop(self) -> None:
        if not self._task:
            return
        self._task.cancel()
        with suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    def wake(self) -> None:
        self._wake_event.set()

    async def _run(self) -> None:
        while True:
            try:
                # Clear before claiming so a wake-up that races with the query
                # remains set and cannot be lost before the wait below.
                self._wake_event.clear()
                job_id = await self.claim_next_job()
                if job_id:
                    await self.execute_claimed_job(job_id)
                    continue

                try:
                    await asyncio.wait_for(
                        self._wake_event.wait(),
                        timeout=settings.job_worker_poll_seconds,
                    )
                except asyncio.TimeoutError:
                    pass
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"❌ Durable worker loop error: {type(exc).__name__}")
                await asyncio.sleep(settings.job_worker_poll_seconds)

    async def claim_next_job(self) -> UUID | None:
        """Atomically claim one due or stale job using a database row lock."""
        now = utc_now()
        cleanup_due = (
            self._last_cleanup_at is None
            or self._last_cleanup_at <= now - timedelta(minutes=1)
        )
        claimed_job_id = None

        async with AsyncSessionLocal() as db:
            async with db.begin():
                if cleanup_due:
                    await self._expire_unclaimable_jobs(db, now)
                result = await db.execute(claimable_job_query(now))
                job = result.scalar_one_or_none()
                if job:
                    apply_claim(job, now)
                    claimed_job_id = job.id

        if cleanup_due:
            self._last_cleanup_at = now
        return claimed_job_id

    async def _expire_unclaimable_jobs(self, db, now: datetime) -> None:
        result = await db.execute(
            select(ExtractionJob)
            .where(
                ExtractionJob.status.in_(tuple(ACTIVE_JOB_STATUSES)),
                or_(
                    ExtractionJob.expires_at <= now,
                    ExtractionJob.attempt_count >= ExtractionJob.max_attempts,
                ),
            )
            .with_for_update(skip_locked=True)
        )
        for job in result.scalars().all():
            if job.expires_at and job.expires_at <= now:
                job.status = "expired"
                job.error_code = "JOB_EXPIRED"
                job.message = "Extraction expired before it could complete"
            else:
                job.status = "failed"
                job.error_code = "MAX_ATTEMPTS_EXCEEDED"
                job.message = "Extraction failed after multiple attempts"
            job.current_step = "error"
            job.completed_at = now
            job.next_attempt_at = None
            job.lease_token = None
            job.leased_until = None
            job.updated_at = now

    async def execute_claimed_job(self, job_id: UUID) -> None:
        """Dispatch one claimed job using only its persisted request payload."""
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(ExtractionJob)
                .where(ExtractionJob.id == job_id)
                .with_for_update()
            )
            job = result.scalar_one_or_none()
            if not job or job.status != "claimed":
                return

            if job.job_kind == "extract" and job.recipe_id:
                apply_recovered_completion(job, utc_now())
                await db.commit()
                return

            job.status = "processing"
            job.current_step = "initializing"
            job.message = "Starting extraction..."
            job.heartbeat_at = utc_now()
            job.leased_until = utc_now() + timedelta(seconds=settings.job_lease_seconds)
            await db.commit()

            persisted = {
                "job_kind": job.job_kind,
                "url": job.url,
                "location": job.location,
                "notes": job.notes,
                "user_id": job.user_id,
                "display_name": job.requested_display_name,
                "is_public": job.requested_is_public,
                "target_recipe_id": job.target_recipe_id,
                "lease_token": job.lease_token,
            }

        heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(job_id, persisted["lease_token"]),
            name=f"extraction-heartbeat-{job_id}",
        )
        try:
            # Lazy import avoids a router/worker import cycle while the existing
            # extraction implementations are incrementally moved into services.
            from app.routers.extract import run_extraction_job, run_re_extraction_job

            if persisted["job_kind"] == "reextract":
                await run_re_extraction_job(
                    job_id=str(job_id),
                    recipe_id=str(persisted["target_recipe_id"]),
                    source_url=persisted["url"],
                    location=persisted["location"],
                    user_id=persisted["user_id"],
                    lease_token=persisted["lease_token"],
                )
            else:
                await run_extraction_job(
                    job_id=str(job_id),
                    url=persisted["url"],
                    location=persisted["location"],
                    notes=persisted["notes"],
                    user_id=persisted["user_id"],
                    user_display_name=persisted["display_name"],
                    is_public=persisted["is_public"],
                    lease_token=persisted["lease_token"],
                )
        except asyncio.CancelledError:
            # Leave the persisted processing lease intact. A new worker will
            # recover the job after lease expiry without losing the request.
            raise
        except Exception as exc:
            await self.retry_or_fail(
                job_id,
                type(exc).__name__,
                expected_lease_token=persisted["lease_token"],
            )
        finally:
            heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat_task

    async def _heartbeat_loop(self, job_id: UUID, lease_token: str) -> None:
        """Renew a live worker's lease independently of progress callbacks."""
        interval = max(10.0, min(30.0, settings.job_lease_seconds / 3))
        while True:
            await asyncio.sleep(interval)
            now = utc_now()
            try:
                async with AsyncSessionLocal() as db:
                    result = await db.execute(
                        select(ExtractionJob).where(
                            ExtractionJob.id == job_id,
                            ExtractionJob.status == "processing",
                            ExtractionJob.lease_token == lease_token,
                        )
                    )
                    job = result.scalar_one_or_none()
                    if not job:
                        return
                    job.heartbeat_at = now
                    job.leased_until = now + timedelta(seconds=settings.job_lease_seconds)
                    job.updated_at = now
                    await db.commit()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # A transient database failure should not kill the extraction.
                # Lease fencing still prevents this worker from committing if a
                # different worker claims the row before connectivity returns.
                print(f"Durable worker heartbeat error: {type(exc).__name__}")

    async def retry_or_fail(
        self,
        job_id: UUID | str,
        error_code: str,
        *,
        expected_lease_token: str | None = None,
    ) -> None:
        """Retry unexpected worker failures with bounded exponential backoff."""
        now = utc_now()
        async with AsyncSessionLocal() as db:
            query = select(ExtractionJob).where(ExtractionJob.id == job_id)
            if expected_lease_token is not None:
                query = query.where(ExtractionJob.lease_token == expected_lease_token)
            result = await db.execute(query.with_for_update())
            job = result.scalar_one_or_none()
            if not job or job.status in TERMINAL_JOB_STATUSES:
                return

            apply_retry_policy(job, now, error_code)
            await db.commit()
        self.wake()

    async def queue_metrics(self) -> dict[str, int]:
        """Return bounded operational counts for admin diagnostics."""
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(ExtractionJob.status, func.count(ExtractionJob.id)).group_by(
                    ExtractionJob.status
                )
            )
            return {status: count for status, count in result.all()}


job_worker = DurableJobWorker()
