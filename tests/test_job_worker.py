from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy.dialects import postgresql

from app.job_worker import (
    ACTIVE_JOB_STATUSES,
    TERMINAL_JOB_STATUSES,
    apply_claim,
    apply_recovered_completion,
    apply_retry_policy,
    claimable_job_query,
    missing_worker_columns,
    settings,
    should_retry_extraction_error,
)
from app.models.recipe import ExtractionJob


def _job(*, attempt_count: int = 0, max_attempts: int = 3) -> ExtractionJob:
    return ExtractionJob(
        id=uuid4(),
        url="https://example.com/recipe",
        user_id="user_test",
        location="Guam",
        notes="",
        status="queued",
        progress=0,
        current_step="queued",
        message="Queued",
        attempt_count=attempt_count,
        max_attempts=max_attempts,
    )


def test_claim_assigns_unique_lease_and_increments_attempt():
    now = datetime(2026, 8, 18, tzinfo=timezone.utc)
    first = _job()
    second = _job()

    apply_claim(first, now)
    apply_claim(second, now)

    assert first.status == "claimed"
    assert first.attempt_count == 1
    assert first.lease_token and first.lease_token != second.lease_token
    assert first.leased_until == now + timedelta(seconds=settings.job_lease_seconds)
    assert first.heartbeat_at == now


def test_unexpected_failure_requeues_with_bounded_backoff():
    now = datetime(2026, 8, 18, tzinfo=timezone.utc)
    job = _job(attempt_count=1)
    job.lease_token = "lease"
    job.leased_until = now + timedelta(minutes=5)

    apply_retry_policy(job, now, "ProviderTimeout")

    assert job.status == "queued"
    assert job.current_step == "retrying"
    assert job.error_code == "ProviderTimeout"
    assert job.next_attempt_at == now + timedelta(seconds=15)
    assert job.lease_token is None
    assert job.leased_until is None


def test_final_attempt_becomes_terminal_failure():
    now = datetime(2026, 8, 18, tzinfo=timezone.utc)
    job = _job(attempt_count=3, max_attempts=3)

    apply_retry_policy(job, now, "UnexpectedFailure")

    assert job.status == "failed"
    assert job.completed_at == now
    assert job.next_attempt_at is None


def test_recipe_link_makes_replay_idempotently_complete():
    now = datetime(2026, 8, 18, tzinfo=timezone.utc)
    job = _job(attempt_count=2)
    job.recipe_id = uuid4()
    job.status = "claimed"
    job.progress = 72
    job.lease_token = "lease"

    apply_recovered_completion(job, now)

    assert job.status == "completed"
    assert job.progress == 100
    assert job.recipe_id is not None
    assert job.lease_token is None
    assert job.completed_at == now


def test_job_state_sets_are_explicit_and_disjoint():
    assert ACTIVE_JOB_STATUSES == {"queued", "claimed", "processing"}
    assert TERMINAL_JOB_STATUSES == {"completed", "failed", "cancelled", "expired"}
    assert ACTIVE_JOB_STATUSES.isdisjoint(TERMINAL_JOB_STATUSES)


def test_only_transient_extraction_failures_are_retried():
    assert should_retry_extraction_error("TIMEOUT") is True
    assert should_retry_extraction_error("llm_extraction_failed") is True
    assert should_retry_extraction_error("VIDEO_PRIVATE") is False
    assert should_retry_extraction_error("INSTAGRAM_AUTH_REQUIRED") is False
    assert should_retry_extraction_error(None) is False


def test_claim_query_uses_postgres_skip_locked_and_stale_lease_recovery():
    now = datetime(2026, 8, 18, tzinfo=timezone.utc)
    sql = str(
        claimable_job_query(now).compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )

    assert "FOR UPDATE SKIP LOCKED" in sql
    assert "extraction_jobs.status = 'queued'" in sql
    assert "extraction_jobs.status IN ('claimed', 'processing')" in sql
    assert "extraction_jobs.leased_until <" in sql
    assert "extraction_jobs.attempt_count < extraction_jobs.max_attempts" in sql


def test_worker_schema_preflight_requires_migration_017_columns():
    assert missing_worker_columns({"id", "job_kind", "lease_token"}) == [
        "attempt_count",
        "expires_at",
        "idempotency_key",
        "leased_until",
    ]
    assert missing_worker_columns(
        {
            "job_kind",
            "lease_token",
            "leased_until",
            "attempt_count",
            "expires_at",
            "idempotency_key",
        }
    ) == []
