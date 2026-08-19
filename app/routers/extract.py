"""Recipe extraction API endpoints."""

import base64
import re
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal, Optional
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.auth import ClerkUser, get_current_user
from app.config import get_settings
from app.db import get_db
from app.image_validation import ImageValidationError, validate_image_bytes
from app.job_worker import (
    ACTIVE_JOB_STATUSES,
    job_worker,
    should_retry_extraction_error,
)
from app.models.recipe import ExtractionJob, Recipe, RecipeVersion
from app.services import recipe_extractor, storage_service, video_service
from app.services.extractor import ExtractionProgress
from app.services.llm_client import llm_service

MAX_OCR_IMAGE_BYTES = 10 * 1024 * 1024
MAX_OCR_TOTAL_BYTES = 40 * 1024 * 1024
settings = get_settings()


class ExtractionJobCancelled(Exception):
    """Stop work when the persisted job has reached cancelled state."""


def _normalized_idempotency_key(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        raise HTTPException(status_code=400, detail="Idempotency-Key cannot be blank")
    return normalized


def _validate_idempotent_job(
    job: ExtractionJob,
    *,
    job_kind: str,
    url: str,
    location: str,
    notes: str,
    is_public: bool,
    display_name: str | None = None,
    target_recipe_id: UUID | None = None,
) -> None:
    """Reject reuse of an idempotency key for a different operation."""
    if not _job_payload_matches(
        job,
        job_kind=job_kind,
        url=url,
        location=location,
        notes=notes,
        is_public=is_public,
        display_name=display_name,
        target_recipe_id=target_recipe_id,
    ):
        raise HTTPException(
            status_code=409,
            detail="Idempotency-Key was already used for a different request",
        )


def _job_payload_matches(
    job: ExtractionJob,
    *,
    job_kind: str,
    url: str,
    location: str,
    notes: str,
    is_public: bool,
    display_name: str | None = None,
    target_recipe_id: UUID | None = None,
) -> bool:
    """Compare the persisted request fields that affect extraction output."""
    return (
        job.job_kind == job_kind
        and job.url == url
        and job.location == location
        and job.notes == notes
        and job.requested_is_public == is_public
        and (display_name is None or job.requested_display_name == display_name)
        and job.target_recipe_id == target_recipe_id
    )


def _require_matching_active_job(
    job: ExtractionJob,
    *,
    job_kind: str,
    url: str,
    location: str,
    notes: str,
    is_public: bool,
    display_name: str | None = None,
    target_recipe_id: UUID | None = None,
) -> None:
    if not _job_payload_matches(
        job,
        job_kind=job_kind,
        url=url,
        location=location,
        notes=notes,
        is_public=is_public,
        display_name=display_name,
        target_recipe_id=target_recipe_id,
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                "An extraction for this source is already running with different options. "
                "Cancel it before starting another."
            ),
        )


def _parse_time_to_minutes(time_str: str) -> Optional[int]:
    """Parse time string like '30 minutes', '1 hour', '1h 30m' to minutes."""
    if not time_str:
        return None
    
    time_str = time_str.lower().strip()
    total_minutes = 0
    
    hours_match = re.search(r'(\d+)\s*(?:hours?|hrs?|h)', time_str)
    if hours_match:
        total_minutes += int(hours_match.group(1)) * 60
    
    mins_match = re.search(r'(\d+)\s*(?:minutes?|mins?|m(?!onth))', time_str)
    if mins_match:
        total_minutes += int(mins_match.group(1))
    
    if total_minutes == 0:
        num_match = re.search(r'(\d+)', time_str)
        if num_match:
            total_minutes = int(num_match.group(1))
    
    return total_minutes if total_minutes > 0 else None


def _compute_total_minutes(extracted: dict) -> Optional[int]:
    """Compute total_minutes from extracted recipe data for SQL filtering."""
    if not extracted:
        return None
    
    times = extracted.get("times") or {}
    total_time = times.get("total") or extracted.get("total_time")
    
    if total_time:
        return _parse_time_to_minutes(str(total_time))
    
    return None


def _generate_reextract_change_summary(old_extracted: dict, new_extracted: dict) -> str:
    """Generate a detailed change summary for re-extraction."""
    if not old_extracted or not new_extracted:
        return "Re-extracted with AI"
    
    changes = []
    
    # Compare title
    old_title = old_extracted.get("title", "")
    new_title = new_extracted.get("title", "")
    if old_title != new_title:
        old_short = old_title[:30] + "..." if len(old_title) > 30 else old_title
        new_short = new_title[:30] + "..." if len(new_title) > 30 else new_title
        changes.append(f'Title: "{old_short}" → "{new_short}"')
    
    # Compare servings
    old_servings = old_extracted.get("servings")
    new_servings = new_extracted.get("servings")
    if old_servings != new_servings:
        changes.append(f"Servings: {old_servings or 'none'} → {new_servings or 'none'}")
    
    # Compare ingredients in detail
    old_ingredients = old_extracted.get("ingredients", [])
    new_ingredients = new_extracted.get("ingredients", [])
    if old_ingredients != new_ingredients:
        ing_changes = _compare_ingredients_detail(old_ingredients, new_ingredients)
        changes.extend(ing_changes)
    
    # Compare steps in detail
    old_steps = old_extracted.get("steps", [])
    new_steps = new_extracted.get("steps", [])
    if old_steps != new_steps:
        step_changes = _compare_steps_detail(old_steps, new_steps)
        changes.extend(step_changes)
    
    # Compare times
    old_times = old_extracted.get("times") or {}
    new_times = new_extracted.get("times") or {}
    if old_times != new_times:
        time_changes = []
        for key, label in [("prep", "prep"), ("cook", "cook"), ("total", "total")]:
            if old_times.get(key) != new_times.get(key):
                time_changes.append(label)
        if time_changes:
            changes.append(f"Times: {', '.join(time_changes)}")
    
    # Compare nutrition
    old_nutrition = old_extracted.get("nutrition", {}).get("perServing", {})
    new_nutrition = new_extracted.get("nutrition", {}).get("perServing", {})
    if old_nutrition != new_nutrition:
        changes.append("Updated nutrition info")
    
    if not changes:
        return "Re-extracted with AI (no significant changes)"
    
    # Limit to 6 changes to avoid overly long summaries
    if len(changes) > 6:
        return "Re-extracted with AI:\n" + "\n".join(changes[:6]) + f"\n... and {len(changes) - 6} more"
    
    return "Re-extracted with AI:\n" + "\n".join(changes)


def _compare_ingredients_detail(old_ingredients: list, new_ingredients: list) -> list:
    """Compare ingredient lists and return detailed changes."""
    changes = []
    
    # Build lookup by name for comparison
    old_by_name = {ing.get("name", "").lower(): ing for ing in old_ingredients}
    new_by_name = {ing.get("name", "").lower(): ing for ing in new_ingredients}
    
    old_names = set(old_by_name.keys())
    new_names = set(new_by_name.keys())
    
    # Find added ingredients
    added = new_names - old_names
    if added:
        if len(added) <= 2:
            for name in list(added)[:2]:
                for ing in new_ingredients:
                    if ing.get("name", "").lower() == name:
                        changes.append(f"Added: {ing.get('name')}")
                        break
        else:
            changes.append(f"Added {len(added)} ingredients")
    
    # Find removed ingredients
    removed = old_names - new_names
    if removed:
        if len(removed) <= 2:
            for name in list(removed)[:2]:
                for ing in old_ingredients:
                    if ing.get("name", "").lower() == name:
                        changes.append(f"Removed: {ing.get('name')}")
                        break
        else:
            changes.append(f"Removed {len(removed)} ingredients")
    
    # Find modified ingredients
    common = old_names & new_names
    modified = []
    for name in common:
        old_ing = old_by_name[name]
        new_ing = new_by_name[name]
        if old_ing != new_ing:
            for ing in new_ingredients:
                if ing.get("name", "").lower() == name:
                    modified.append(ing.get("name"))
                    break
    
    if modified:
        if len(modified) <= 2:
            for name in modified[:2]:
                changes.append(f"Modified: {name}")
        else:
            changes.append(f"Modified {len(modified)} ingredients")
    
    return changes


def _compare_steps_detail(old_steps: list, new_steps: list) -> list:
    """Compare step lists and return detailed changes."""
    changes = []
    
    old_count = len(old_steps)
    new_count = len(new_steps)
    
    if new_count > old_count:
        changes.append(f"Added {new_count - old_count} step(s)")
    elif new_count < old_count:
        changes.append(f"Removed {old_count - new_count} step(s)")
    
    # Check for modified steps
    min_count = min(old_count, new_count)
    modified_steps = []
    for i in range(min_count):
        if old_steps[i] != new_steps[i]:
            modified_steps.append(i + 1)
    
    if modified_steps:
        if len(modified_steps) <= 3:
            changes.append(f"Modified step(s): {', '.join(map(str, modified_steps))}")
        else:
            changes.append(f"Modified {len(modified_steps)} steps")
    
    return changes
router = APIRouter(prefix="/api", tags=["extraction"])


async def _user_can_access_job(db: AsyncSession, job: ExtractionJob | None, user: ClerkUser) -> bool:
    """Return True when a job belongs to the user.

    Legacy jobs created before migration 015 have a NULL user_id. To avoid stranding
    in-flight clients during deploy, claim those unowned jobs on the first
    authenticated poll/cancel. If the job already links to a recipe, only the
    recipe owner may claim it.
    """
    if not job:
        return False

    if job.user_id == user.id:
        return True

    if job.user_id is not None:
        return False

    if job.recipe_id:
        recipe_result = await db.execute(select(Recipe.user_id).where(Recipe.id == job.recipe_id))
        recipe_user_id = recipe_result.scalar_one_or_none()
        if recipe_user_id and recipe_user_id != user.id:
            return False

    claim_result = await db.execute(
        update(ExtractionJob)
        .where(
            ExtractionJob.id == job.id,
            ExtractionJob.user_id.is_(None),
        )
        .values(user_id=user.id, updated_at=datetime.utcnow())
        .returning(ExtractionJob.id)
    )

    if not claim_result.scalar_one_or_none():
        await db.rollback()
        return False

    await db.commit()
    await db.refresh(job)
    return job.user_id == user.id


# Request/Response models
class ExtractRequest(BaseModel):
    """Request to extract a recipe from URL."""
    url: str = Field(min_length=1, max_length=2_048)
    location: str = Field(default="Guam", min_length=1, max_length=100)
    notes: str = Field(default="", max_length=4_000)
    quick_check: bool = False  # If true, only check for existing
    is_public: bool = False  # Private unless the user explicitly publishes it


class ExtractResponse(BaseModel):
    """Response from extraction."""
    id: UUID
    recipe: dict
    is_existing: bool = False


class JobStatusResponse(BaseModel):
    """Status of an extraction job."""
    id: UUID
    url: str
    status: Literal["queued", "claimed", "processing", "completed", "failed", "cancelled", "expired"]
    progress: int
    current_step: str
    message: str
    recipe_id: Optional[UUID] = None
    error_message: Optional[str] = None
    error_code: Optional[str] = None
    attempt_count: int = 0
    max_attempts: int = 3
    next_attempt_at: Optional[datetime] = None
    low_confidence: bool = False  # True if extraction quality is uncertain
    confidence_warning: Optional[str] = None  # Warning message for user


@router.post("/extract", response_model=ExtractResponse)
async def extract_recipe(
    request: ExtractRequest,
    db: AsyncSession = Depends(get_db),
    user: ClerkUser = Depends(get_current_user),
):
    """
    Extract a recipe from a video URL.
    
    Supports TikTok, YouTube, and Instagram videos.
    
    If the user already has a recipe with this URL, returns the existing recipe.
    """
    url = request.url.strip()
    
    # Check for existing recipe FROM THIS USER
    result = await db.execute(
        select(Recipe).where(
            Recipe.source_url == url,
            Recipe.user_id == user.id
        )
    )
    existing = result.scalar_one_or_none()
    
    if existing:
        return ExtractResponse(
            id=existing.id,
            recipe=existing.extracted,
            is_existing=True
        )
    
    # Quick check mode - just return not found
    if request.quick_check:
        raise HTTPException(
            status_code=404,
            detail="Recipe not found"
        )
    
    # Detect platform
    platform = video_service.detect_platform(url)
    
    if platform == "web":
        # Website extraction (recipe blogs, etc.)
        from app.services.website import website_service
        
        extraction_result = await website_service.extract(
            url=url,
            location=request.location,
            notes=request.notes
        )
        
        if not extraction_result.success:
            # Website service already provides friendly error messages
            raise HTTPException(
                status_code=400 if extraction_result.error_type == "invalid_url" else 500,
                detail=extraction_result.error or "We couldn't extract a recipe from this website.",
            )
        
        # Save to database
        new_recipe = Recipe(
            source_url=url,
            source_type="website",
            raw_text=extraction_result.raw_text,
            extracted=extraction_result.recipe,
            thumbnail_url=extraction_result.thumbnail_url,
            extraction_method=extraction_result.extraction_method,
            extraction_quality=extraction_result.extraction_quality,
            has_audio_transcript=False,
            user_id=user.id,
            extractor_display_name=user.display_name,
            is_public=request.is_public,
            total_minutes=_compute_total_minutes(extraction_result.recipe),
        )
        
        db.add(new_recipe)
        await db.commit()
        await db.refresh(new_recipe)
        
        # Upload thumbnail to S3 for permanent storage
        if extraction_result.thumbnail_url:
            s3_url = await storage_service.upload_thumbnail_from_url(
                extraction_result.thumbnail_url,
                str(new_recipe.id)
            )
            if s3_url:
                new_recipe.thumbnail_url = s3_url
                if new_recipe.extracted and "media" in new_recipe.extracted:
                    new_recipe.extracted["media"]["thumbnail"] = s3_url
                await db.commit()
                await db.refresh(new_recipe)
        
        return ExtractResponse(
            id=new_recipe.id,
            recipe=new_recipe.extracted,
            is_existing=False
        )
    
    # Video extraction (TikTok, YouTube, Instagram)
    extraction_result = await recipe_extractor.extract(
        url=url,
        location=request.location,
        notes=request.notes
    )
    
    if not extraction_result.success:
        # Use friendly error if available
        error_detail = extraction_result.friendly_error or extraction_result.error or "Extraction failed"
        raise HTTPException(
            status_code=500,
            detail=error_detail
        )
    
    # Save to database with user_id and display name
    new_recipe = Recipe(
        source_url=url,
        source_type=platform,
        raw_text=extraction_result.raw_text,
        extracted=extraction_result.recipe,
        thumbnail_url=extraction_result.thumbnail_url,
        extraction_method=extraction_result.extraction_method,
        extraction_quality=extraction_result.extraction_quality,
        has_audio_transcript=extraction_result.has_audio_transcript,
        user_id=user.id,  # Assign to current user
        extractor_display_name=user.display_name,  # Store display name for attribution
        is_public=request.is_public,
        total_minutes=_compute_total_minutes(extraction_result.recipe),
    )
    
    db.add(new_recipe)
    await db.commit()
    await db.refresh(new_recipe)
    
    # Upload thumbnail to S3 for permanent storage
    if extraction_result.thumbnail_url:
        s3_url = await storage_service.upload_thumbnail_from_url(
            extraction_result.thumbnail_url,
            str(new_recipe.id)
        )
        if s3_url:
            # Update recipe with S3 URL
            new_recipe.thumbnail_url = s3_url
            # Also update the media field in extracted JSON
            if new_recipe.extracted and "media" in new_recipe.extracted:
                new_recipe.extracted["media"]["thumbnail"] = s3_url
            await db.commit()
            await db.refresh(new_recipe)
    
    return ExtractResponse(
        id=new_recipe.id,
        recipe=new_recipe.extracted,
        is_existing=False
    )


@router.post("/extract/async")
async def start_extraction_job(
    request: ExtractRequest,
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key", min_length=1, max_length=128),
    ] = None,
    db: AsyncSession = Depends(get_db),
    user: ClerkUser = Depends(get_current_user),
):
    """
    Start an async extraction job.
    
    Returns immediately with a job ID that can be polled for status.
    URLs are normalized (e.g., TikTok short URLs resolved) before storing.
    """
    from app.services.video import VideoService
    
    original_url = request.url.strip()
    idempotency_key = _normalized_idempotency_key(idempotency_key)
    
    # Normalize the URL (resolve TikTok short URLs, etc.)
    url = await VideoService.normalize_url(original_url)
    print(f"📎 Normalized URL: {original_url} → {url}")

    if idempotency_key:
        idempotent_result = await db.execute(
            select(ExtractionJob).where(
                ExtractionJob.user_id == user.id,
                ExtractionJob.idempotency_key == idempotency_key,
            )
        )
        idempotent_job = idempotent_result.scalar_one_or_none()
        if idempotent_job:
            _validate_idempotent_job(
                idempotent_job,
                job_kind="extract",
                url=url,
                location=request.location,
                notes=request.notes,
                is_public=request.is_public,
                display_name=user.display_name,
            )
            return {
                "job_id": str(idempotent_job.id),
                "status": idempotent_job.status,
                "recipe_id": str(idempotent_job.recipe_id) if idempotent_job.recipe_id else None,
                "is_existing": True,
            }
    
    # Check for existing recipe FROM THIS USER (check both original and normalized)
    result = await db.execute(
        select(Recipe).where(
            or_(Recipe.source_url == original_url, Recipe.source_url == url),
            Recipe.user_id == user.id
        )
    )
    existing = result.scalar_one_or_none()
    
    if existing:
        return {
            "job_id": None,
            "status": "completed",
            "recipe_id": str(existing.id),
            "is_existing": True
        }
    
    # Check for an existing extraction job for this URL from this user
    job_result = await db.execute(
        select(ExtractionJob)
        .where(
            or_(ExtractionJob.url == original_url, ExtractionJob.url == url),
            ExtractionJob.user_id == user.id,
            ExtractionJob.job_kind == "extract",
        )
        .order_by(ExtractionJob.created_at.desc())
        .limit(1)
    )
    existing_job = job_result.scalar_one_or_none()
    
    if existing_job:
        if existing_job.status in ACTIVE_JOB_STATUSES:
            _require_matching_active_job(
                existing_job,
                job_kind="extract",
                url=url,
                location=request.location,
                notes=request.notes,
                is_public=request.is_public,
                display_name=user.display_name,
            )
            return {
                "job_id": str(existing_job.id),
                "status": existing_job.status,
                "message": "Extraction already in progress"
            }
    
    # Create new job record
    job_id = uuid4()
    
    # Store job in database
    job = ExtractionJob(
        id=job_id,
        url=url,
        user_id=user.id,
        location=request.location,
        notes=request.notes,
        status="queued",
        job_kind="extract",
        requested_is_public=request.is_public,
        requested_display_name=user.display_name,
        idempotency_key=idempotency_key,
        progress=0,
        current_step="queued",
        message="Queued for extraction",
        max_attempts=settings.job_max_attempts,
        next_attempt_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=settings.job_expiry_hours),
    )

    try:
        db.add(job)
        await db.commit()
    except IntegrityError:
        await db.rollback()
        if idempotency_key:
            idempotent_result = await db.execute(
                select(ExtractionJob).where(
                    ExtractionJob.user_id == user.id,
                    ExtractionJob.idempotency_key == idempotency_key,
                )
            )
            idempotent_job = idempotent_result.scalar_one_or_none()
            if idempotent_job:
                _validate_idempotent_job(
                    idempotent_job,
                    job_kind="extract",
                    url=url,
                    location=request.location,
                    notes=request.notes,
                    is_public=request.is_public,
                    display_name=user.display_name,
                )
                return {
                    "job_id": str(idempotent_job.id),
                    "status": idempotent_job.status,
                    "recipe_id": (
                        str(idempotent_job.recipe_id) if idempotent_job.recipe_id else None
                    ),
                    "is_existing": True,
                }
        race_result = await db.execute(
            select(ExtractionJob)
            .where(
                ExtractionJob.user_id == user.id,
                ExtractionJob.url == url,
                ExtractionJob.job_kind == "extract",
                ExtractionJob.status.in_(tuple(ACTIVE_JOB_STATUSES)),
            )
            .order_by(ExtractionJob.created_at.desc())
            .limit(1)
        )
        raced_job = race_result.scalar_one_or_none()
        if not raced_job:
            raise
        _require_matching_active_job(
            raced_job,
            job_kind="extract",
            url=url,
            location=request.location,
            notes=request.notes,
            is_public=request.is_public,
            display_name=user.display_name,
        )
        return {
            "job_id": str(raced_job.id),
            "status": raced_job.status,
            "message": "Extraction already in progress",
        }

    job_worker.wake()
    
    return {
        "job_id": str(job_id),
        "status": "queued",
        "message": "Extraction queued"
    }


async def run_extraction_job(
    job_id: str,
    url: str,
    location: str,
    notes: str,
    user_id: str,  # User ID for the recipe
    user_display_name: str = "A chef",  # Display name for attribution
    is_public: bool = False,  # Private unless the user explicitly publishes it
    lease_token: str | None = None,
):
    """Background task to run extraction."""
    from app.db.database import AsyncSessionLocal
    
    async with AsyncSessionLocal() as db:
        try:
            # Update progress callback
            async def update_progress(progress):
                job_result = await db.execute(
                    select(ExtractionJob)
                    .where(
                        ExtractionJob.id == job_id,
                        ExtractionJob.lease_token == lease_token,
                    )
                    .execution_options(populate_existing=True)
                )
                job = job_result.scalar_one_or_none()
                if not job or job.status == "cancelled":
                    raise ExtractionJobCancelled
                job.progress = progress.progress
                job.current_step = progress.step
                job.message = progress.message
                job.heartbeat_at = datetime.now(timezone.utc)
                job.leased_until = datetime.now(timezone.utc) + timedelta(
                    seconds=settings.job_lease_seconds
                )
                job.updated_at = datetime.now(timezone.utc)
                await db.commit()
            
            # Detect platform and run appropriate extraction
            platform = video_service.detect_platform(url)
            
            if platform == "web":
                # Website extraction
                from app.services.website import website_service
                
                await update_progress(ExtractionProgress(
                    step="fetching",
                    progress=20,
                    message="Fetching webpage..."
                ))
                
                result = await website_service.extract(
                    url=url,
                    location=location,
                    notes=notes
                )
                platform = "website"  # Use "website" as source_type
            else:
                # Video extraction (TikTok, YouTube, Instagram)
                result = await recipe_extractor.extract(
                    url=url,
                    location=location,
                    notes=notes,
                    progress_callback=update_progress
                )
            
            # Get job record
            job_result = await db.execute(
                select(ExtractionJob)
                .where(
                    ExtractionJob.id == job_id,
                    ExtractionJob.lease_token == lease_token,
                )
                .execution_options(populate_existing=True)
            )
            job = job_result.scalar_one_or_none()
            
            if not job:
                print(f"❌ Job {job_id} not found")
                return
            
            # Check if job was cancelled before saving (early check)
            if job.status == "cancelled":
                print(f"🚫 Job {job_id} was cancelled - not saving recipe")
                return
            
            if result.success:
                # CRITICAL: Re-check cancellation status with FRESH data before saving
                # This prevents race condition where cancel comes in during extraction
                # Use a new query with execution_options to get fresh data from DB
                fresh_job_result = await db.execute(
                    select(ExtractionJob)
                    .where(
                        ExtractionJob.id == job_id,
                        ExtractionJob.lease_token == lease_token,
                    )
                    .execution_options(populate_existing=True)
                )
                job = fresh_job_result.scalar_one_or_none()
                
                if not job or job.status == "cancelled":
                    print(f"🚫 Job {job_id} was cancelled during extraction - not saving recipe")
                    return
                
                # Add confidence info to extracted JSON if low confidence
                extracted_data = result.recipe.copy() if result.recipe else {}
                if result.low_confidence:
                    extracted_data['lowConfidence'] = True
                    extracted_data['confidenceWarning'] = result.confidence_warning
                
                # Keep a copy of extracted_data before any DB operations
                # This protects against session state issues
                saved_extracted = dict(extracted_data)
                
                # Save recipe WITH USER ID and display name
                new_recipe = Recipe(
                    source_url=url,
                    source_type=platform,
                    raw_text=result.raw_text,
                    extracted=extracted_data,
                    thumbnail_url=result.thumbnail_url,
                    extraction_method=result.extraction_method,
                    extraction_quality=result.extraction_quality,
                    has_audio_transcript=result.has_audio_transcript,
                    user_id=user_id,  # Assign to user
                    extractor_display_name=user_display_name,  # Store display name
                    is_public=is_public,
                    total_minutes=_compute_total_minutes(extracted_data),
                )
                db.add(new_recipe)
                await db.flush()
                # Commit the recipe and durable job link atomically. If the
                # process exits afterward, stale recovery completes this job
                # instead of creating a duplicate recipe.
                job.recipe_id = new_recipe.id
                await db.commit()
                await db.refresh(new_recipe)
                
                # Check AGAIN after commit - if cancelled during save, delete the recipe
                post_save_job_result = await db.execute(
                    select(ExtractionJob)
                    .where(ExtractionJob.id == job_id)
                    .execution_options(populate_existing=True)
                )
                job = post_save_job_result.scalar_one_or_none()
                if job and job.status == "cancelled":
                    print(f"🚫 Job {job_id} was cancelled during save - deleting recipe {new_recipe.id}")
                    await db.delete(new_recipe)
                    job.recipe_id = None
                    await db.commit()
                    return
                if not job or job.lease_token != lease_token:
                    # Another worker owns this lease now. It will observe the
                    # durable recipe link and complete without duplicating it.
                    return
                
                # Upload thumbnail to S3 for permanent storage
                if result.thumbnail_url:
                    await update_progress(ExtractionProgress(
                        step="saving",
                        progress=85,
                        message="Saving thumbnail..."
                    ))
                    s3_url = await storage_service.upload_thumbnail_from_url(
                        result.thumbnail_url,
                        str(new_recipe.id)
                    )
                    if s3_url:
                        # Update recipe with S3 URL using saved_extracted to preserve lowConfidence
                        new_recipe.thumbnail_url = s3_url
                        if saved_extracted and "media" in saved_extracted:
                            # Update thumbnail in our preserved copy
                            saved_extracted["media"] = dict(saved_extracted.get("media", {}))
                            saved_extracted["media"]["thumbnail"] = s3_url
                            new_recipe.extracted = saved_extracted
                            flag_modified(new_recipe, 'extracted')
                        await db.commit()
                
                # Update job as completed (only NOW, after everything is done)
                # Set completion message based on confidence
                if result.low_confidence:
                    completion_msg = "Recipe extracted - please review for accuracy"
                else:
                    completion_msg = "Recipe extracted successfully!"
                
                await update_progress(ExtractionProgress(
                    step="complete",
                    progress=100,
                    message=completion_msg
                ))

                terminal_job_result = await db.execute(
                    select(ExtractionJob)
                    .where(
                        ExtractionJob.id == job_id,
                        ExtractionJob.lease_token == lease_token,
                    )
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
                job = terminal_job_result.scalar_one_or_none()
                if not job:
                    raise ExtractionJobCancelled

                job.status = "completed"
                job.progress = 100
                job.current_step = "complete"
                job.message = completion_msg
                job.recipe_id = new_recipe.id
                job.completed_at = datetime.utcnow()
                job.low_confidence = result.low_confidence
                job.confidence_warning = result.confidence_warning
                job.lease_token = None
                job.leased_until = None
            else:
                # Use friendly error if available, otherwise raw error
                friendly_msg = result.friendly_error or result.error or "Extraction failed"
                job.error_message = friendly_msg  # Show friendly message to user
                error_code = result.error_code or "EXTRACTION_FAILED"
                job.error_code = error_code
                if should_retry_extraction_error(error_code):
                    await db.commit()
                    await job_worker.retry_or_fail(
                        job_id,
                        error_code,
                        expected_lease_token=lease_token,
                    )
                    return
                job.status = "failed"
                job.current_step = "error"
                job.message = friendly_msg
                job.completed_at = datetime.now(timezone.utc)
                job.lease_token = None
                job.leased_until = None
            
            job.updated_at = datetime.utcnow()
            await db.commit()
            
        except ExtractionJobCancelled:
            await db.rollback()
            cancelled_result = await db.execute(
                select(ExtractionJob).where(ExtractionJob.id == job_id).with_for_update()
            )
            cancelled_job = cancelled_result.scalar_one_or_none()
            if cancelled_job and cancelled_job.status == "cancelled" and cancelled_job.recipe_id:
                recipe_result = await db.execute(
                    select(Recipe).where(Recipe.id == cancelled_job.recipe_id)
                )
                cancelled_recipe = recipe_result.scalar_one_or_none()
                cancelled_job.recipe_id = None
                await db.flush()
                if cancelled_recipe:
                    await db.delete(cancelled_recipe)
                await db.commit()
        except Exception as e:
            await db.rollback()
            print(f"❌ Extraction job {job_id} failed: {type(e).__name__}")
            await job_worker.retry_or_fail(
                job_id,
                type(e).__name__,
                expected_lease_token=lease_token,
            )


@router.get("/jobs/{job_id}", response_model=JobStatusResponse)
async def get_job_status(
    job_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: ClerkUser = Depends(get_current_user),
):
    """Get the status of an extraction job."""
    result = await db.execute(
        select(ExtractionJob).where(ExtractionJob.id == job_id)
    )
    job = result.scalar_one_or_none()
    
    if not await _user_can_access_job(db, job, user):
        raise HTTPException(status_code=404, detail="Job not found")
    
    return JobStatusResponse(
        id=job.id,
        url=job.url,
        status=job.status,
        progress=job.progress,
        current_step=job.current_step,
        message=job.message,
        recipe_id=job.recipe_id,
        error_message=job.error_message,
        error_code=job.error_code,
        attempt_count=job.attempt_count,
        max_attempts=job.max_attempts,
        next_attempt_at=job.next_attempt_at,
        low_confidence=job.low_confidence or False,
        confidence_warning=job.confidence_warning
    )


@router.delete("/jobs/{job_id}")
async def cancel_job(
    job_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: ClerkUser = Depends(get_current_user),
):
    """
    Cancel an extraction job.
    
    This marks the job as 'cancelled'. The background task will check this status
    and avoid saving the recipe if cancelled.
    """
    result = await db.execute(
        select(ExtractionJob)
        .where(ExtractionJob.id == job_id)
        .with_for_update()
    )
    job = result.scalar_one_or_none()
    
    if not await _user_can_access_job(db, job, user):
        raise HTTPException(status_code=404, detail="Job not found")
    
    if job.status not in ACTIVE_JOB_STATUSES:
        raise HTTPException(
            status_code=400, 
            detail=f"Cannot cancel job with status '{job.status}'"
        )
    
    # Mark as cancelled
    job.status = "cancelled"
    job.current_step = "cancelled"
    job.message = "Extraction cancelled by user"
    job.completed_at = datetime.now(timezone.utc)
    job.next_attempt_at = None
    job.lease_token = None
    job.leased_until = None
    job.updated_at = datetime.utcnow()
    await db.commit()
    
    print(f"🚫 Job {job_id} cancelled by user")
    
    return {"message": "Job cancelled successfully", "job_id": str(job_id)}


@router.get("/locations")
async def get_available_locations():
    """Get list of available locations for cost estimation."""
    return {
        "locations": [
            {"code": "guam", "name": "Guam", "description": "25-40% higher than mainland US"},
            {"code": "hawaii", "name": "Hawaii", "description": "20-30% higher than mainland US"},
            {"code": "us", "name": "US Average", "description": "Standard baseline pricing"},
            {"code": "uk", "name": "United Kingdom", "description": "UK pricing (converted to USD)"},
            {"code": "canada", "name": "Canada", "description": "Similar to US pricing"},
            {"code": "australia", "name": "Australia", "description": "AUD converted to USD"},
            {"code": "japan", "name": "Japan", "description": "Yen converted to USD"},
            {"code": "eu", "name": "European Union", "description": "Euro converted to USD"},
        ],
        "default": "Guam"
    }


class ReExtractAsyncRequest(BaseModel):
    """Request to re-extract a recipe asynchronously."""
    location: str = Field(default="Guam", min_length=1, max_length=100)


@router.post("/re-extract/{recipe_id}/async")
async def start_re_extraction_job(
    recipe_id: UUID,
    request: ReExtractAsyncRequest,
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key", min_length=1, max_length=128),
    ] = None,
    db: AsyncSession = Depends(get_db),
    user: ClerkUser = Depends(get_current_user),
):
    """
    Start an async re-extraction job for an existing recipe.
    
    Returns immediately with a job ID that can be polled for status.
    Only allowed for recipe owners or admin users.
    """
    # Fetch the recipe
    result = await db.execute(select(Recipe).where(Recipe.id == recipe_id))
    recipe = result.scalar_one_or_none()
    
    if not recipe:
        raise HTTPException(status_code=404, detail="Recipe not found")
    
    # Check permissions: owner or admin
    is_owner = recipe.user_id == user.id
    is_admin = user.is_admin
    
    if not is_owner and not is_admin:
        raise HTTPException(
            status_code=403, 
            detail="You don't have permission to re-extract this recipe"
        )
    
    # Check if recipe can be re-extracted
    if not recipe.source_url or recipe.source_url.startswith("manual://"):
        raise HTTPException(
            status_code=400,
            detail="Cannot re-extract manual recipes. Please edit them directly."
        )

    idempotency_key = _normalized_idempotency_key(idempotency_key)
    if idempotency_key:
        idempotent_result = await db.execute(
            select(ExtractionJob).where(
                ExtractionJob.user_id == user.id,
                ExtractionJob.idempotency_key == idempotency_key,
            )
        )
        idempotent_job = idempotent_result.scalar_one_or_none()
        if idempotent_job:
            _validate_idempotent_job(
                idempotent_job,
                job_kind="reextract",
                url=recipe.source_url,
                location=request.location,
                notes="",
                is_public=recipe.is_public,
                target_recipe_id=recipe_id,
            )
            return {
                "job_id": str(idempotent_job.id),
                "status": idempotent_job.status,
                "recipe_id": str(recipe_id),
                "is_existing": True,
            }

    job_result = await db.execute(
        select(ExtractionJob)
        .where(
            ExtractionJob.target_recipe_id == recipe_id,
            ExtractionJob.job_kind == "reextract",
            ExtractionJob.user_id == user.id,
        )
        .order_by(ExtractionJob.created_at.desc())
        .limit(1)
    )
    existing_job = job_result.scalar_one_or_none()
    
    if existing_job:
        if existing_job.status in ACTIVE_JOB_STATUSES:
            _require_matching_active_job(
                existing_job,
                job_kind="reextract",
                url=recipe.source_url,
                location=request.location,
                notes="",
                is_public=recipe.is_public,
                target_recipe_id=recipe_id,
            )
            return {
                "job_id": str(existing_job.id),
                "status": existing_job.status,
                "message": "Re-extraction already in progress"
            }
    
    # Create new job record
    job_id = uuid4()
    
    job = ExtractionJob(
        id=job_id,
        url=recipe.source_url,
        user_id=user.id,
        location=request.location,
        notes="",
        status="queued",
        job_kind="reextract",
        target_recipe_id=recipe_id,
        requested_display_name=user.display_name,
        requested_is_public=recipe.is_public,
        idempotency_key=idempotency_key,
        progress=0,
        current_step="queued",
        message="Queued for re-extraction",
        max_attempts=settings.job_max_attempts,
        next_attempt_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=settings.job_expiry_hours),
    )

    try:
        db.add(job)
        await db.commit()
    except IntegrityError:
        await db.rollback()
        if idempotency_key:
            idempotent_result = await db.execute(
                select(ExtractionJob).where(
                    ExtractionJob.user_id == user.id,
                    ExtractionJob.idempotency_key == idempotency_key,
                )
            )
            idempotent_job = idempotent_result.scalar_one_or_none()
            if idempotent_job:
                _validate_idempotent_job(
                    idempotent_job,
                    job_kind="reextract",
                    url=recipe.source_url,
                    location=request.location,
                    notes="",
                    is_public=recipe.is_public,
                    target_recipe_id=recipe_id,
                )
                return {
                    "job_id": str(idempotent_job.id),
                    "status": idempotent_job.status,
                    "recipe_id": str(recipe_id),
                    "is_existing": True,
                }
        race_result = await db.execute(
            select(ExtractionJob)
            .where(
                ExtractionJob.user_id == user.id,
                ExtractionJob.target_recipe_id == recipe_id,
                ExtractionJob.job_kind == "reextract",
                ExtractionJob.status.in_(tuple(ACTIVE_JOB_STATUSES)),
            )
            .order_by(ExtractionJob.created_at.desc())
            .limit(1)
        )
        raced_job = race_result.scalar_one_or_none()
        if not raced_job:
            raise
        _require_matching_active_job(
            raced_job,
            job_kind="reextract",
            url=recipe.source_url,
            location=request.location,
            notes="",
            is_public=recipe.is_public,
            target_recipe_id=recipe_id,
        )
        return {
            "job_id": str(raced_job.id),
            "status": raced_job.status,
            "message": "Re-extraction already in progress",
            "recipe_id": str(recipe_id),
        }

    job_worker.wake()
    
    return {
        "job_id": str(job_id),
        "status": "queued",
        "message": "Re-extraction queued",
        "recipe_id": str(recipe_id)
    }


async def run_re_extraction_job(
    job_id: str,
    recipe_id: str,
    source_url: str,
    location: str,
    user_id: str,
    lease_token: str | None = None,
):
    """Background task to run re-extraction and update existing recipe."""
    from app.db.database import AsyncSessionLocal
    
    async with AsyncSessionLocal() as db:
        try:
            # Update progress callback
            async def update_progress(progress):
                job_result = await db.execute(
                    select(ExtractionJob)
                    .where(
                        ExtractionJob.id == job_id,
                        ExtractionJob.lease_token == lease_token,
                    )
                    .execution_options(populate_existing=True)
                )
                job = job_result.scalar_one_or_none()
                if not job or job.status == "cancelled":
                    raise ExtractionJobCancelled
                job.progress = progress.progress
                job.current_step = progress.step
                job.message = progress.message
                job.heartbeat_at = datetime.now(timezone.utc)
                job.leased_until = datetime.now(timezone.utc) + timedelta(
                    seconds=settings.job_lease_seconds
                )
                job.updated_at = datetime.now(timezone.utc)
                await db.commit()
            
            # Get the existing recipe
            recipe_result = await db.execute(
                select(Recipe).where(Recipe.id == recipe_id)
            )
            recipe = recipe_result.scalar_one_or_none()
            
            if not recipe:
                raise Exception(f"Recipe {recipe_id} not found")
            
            # Save old state BEFORE extraction for version comparison
            old_extracted = dict(recipe.extracted) if recipe.extracted else {}
            old_thumbnail = recipe.thumbnail_url
            
            # Preserve original if not already done
            if not recipe.original_extracted and recipe.extracted:
                recipe.original_extracted = recipe.extracted.copy()
                await db.commit()
            
            # Detect platform and run appropriate extraction
            platform = video_service.detect_platform(source_url)
            
            if platform == "web":
                # Website extraction (recipe blogs, etc.)
                from app.services.website import website_service
                
                await update_progress(ExtractionProgress(
                    step="fetching",
                    progress=20,
                    message="Fetching webpage..."
                ))
                
                result = await website_service.extract(
                    url=source_url,
                    location=location,
                    notes=""
                )
            else:
                # Video extraction (TikTok, YouTube, Instagram) - with audio for best quality
                result = await recipe_extractor.extract(
                    url=source_url,
                    location=location,
                    notes="",
                    progress_callback=update_progress
                )
            
            # Get job record
            job_result = await db.execute(
                select(ExtractionJob)
                .where(
                    ExtractionJob.id == job_id,
                    ExtractionJob.lease_token == lease_token,
                )
                .execution_options(populate_existing=True)
            )
            job = job_result.scalar_one_or_none()
            
            if not job:
                print(f"❌ Re-extraction job {job_id} not found")
                return

            if job.status == "cancelled":
                return
            
            if result.success:
                new_extracted = result.recipe
                
                # Generate change summary comparing old vs new
                change_summary = _generate_reextract_change_summary(old_extracted, new_extracted)
                
                # Create version snapshot with OLD state and change comparison
                version_result = await db.execute(
                    select(func.max(RecipeVersion.version_number))
                    .where(RecipeVersion.recipe_id == recipe.id)
                )
                max_version = version_result.scalar() or 0
                
                version = RecipeVersion(
                    recipe_id=recipe.id,
                    version_number=max_version + 1,
                    extracted=old_extracted,  # Store OLD state
                    thumbnail_url=old_thumbnail,
                    change_type="re-extract",
                    change_summary=change_summary,
                    created_by=user_id,
                )
                db.add(version)
                
                # ============================================================
                # BUILD ALL RECIPE DATA IN MEMORY FIRST, THEN SINGLE COMMIT
                # This avoids session state issues with multiple commits
                # ============================================================
                
                # Make a fresh copy of extracted data
                final_extracted = dict(new_extracted)
                
                # Add confidence info
                if result.low_confidence:
                    final_extracted['lowConfidence'] = True
                    final_extracted['confidenceWarning'] = result.confidence_warning
                    print(f"🔴 Setting lowConfidence=True for recipe {recipe.id}")
                else:
                    final_extracted.pop('lowConfidence', None)
                    final_extracted.pop('confidenceWarning', None)
                
                # Upload thumbnail FIRST (before any DB commits) so we have the URL
                final_thumbnail_url = recipe.thumbnail_url  # Keep existing
                if result.thumbnail_url:
                    await update_progress(ExtractionProgress(
                        step="saving",
                        progress=85,
                        message="Saving thumbnail..."
                    ))
                    s3_url = await storage_service.upload_thumbnail_from_url(
                        result.thumbnail_url,
                        str(recipe.id)
                    )
                    if s3_url:
                        final_thumbnail_url = s3_url
                        # Update thumbnail in extracted data
                        if "media" in final_extracted:
                            final_extracted["media"] = dict(final_extracted.get("media", {}))
                            final_extracted["media"]["thumbnail"] = s3_url

                terminal_job_result = await db.execute(
                    select(ExtractionJob)
                    .where(
                        ExtractionJob.id == job_id,
                        ExtractionJob.lease_token == lease_token,
                    )
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
                job = terminal_job_result.scalar_one_or_none()
                if not job:
                    raise ExtractionJobCancelled
                
                # Now apply ALL changes to the recipe object at once
                print(f"🔵 Final extracted has lowConfidence = {final_extracted.get('lowConfidence')}")
                print(f"🔵 Final extracted keys = {list(final_extracted.keys())}")
                
                recipe.raw_text = result.raw_text
                recipe.extracted = final_extracted
                recipe.thumbnail_url = final_thumbnail_url
                recipe.extraction_method = result.extraction_method
                recipe.extraction_quality = result.extraction_quality
                recipe.has_audio_transcript = result.has_audio_transcript
                
                # Mark as modified for SQLAlchemy
                flag_modified(recipe, 'extracted')
                
                if result.low_confidence:
                    completion_msg = "Recipe re-extracted - please review for accuracy"
                else:
                    completion_msg = "Recipe re-extracted successfully!"

                # Recipe, version snapshot, and terminal job transition commit
                # atomically so stale recovery cannot create another version.
                job.status = "completed"
                job.progress = 100
                job.current_step = "complete"
                job.message = completion_msg
                job.recipe_id = recipe.id
                job.completed_at = datetime.utcnow()
                job.low_confidence = result.low_confidence
                job.confidence_warning = result.confidence_warning
                job.lease_token = None
                job.leased_until = None
                job.updated_at = datetime.now(timezone.utc)
                await db.commit()
                print(f"🟣 After SINGLE commit, lowConfidence = {recipe.extracted.get('lowConfidence')}")
                return
            else:
                # Use friendly error if available, otherwise raw error
                friendly_msg = result.friendly_error or result.error or "Re-extraction failed"
                job.error_message = friendly_msg  # Show friendly message to user
                error_code = result.error_code or "REEXTRACTION_FAILED"
                job.error_code = error_code
                if should_retry_extraction_error(error_code):
                    await db.commit()
                    await job_worker.retry_or_fail(
                        job_id,
                        error_code,
                        expected_lease_token=lease_token,
                    )
                    return
                job.status = "failed"
                job.current_step = "error"
                job.message = friendly_msg
                job.completed_at = datetime.now(timezone.utc)
                job.lease_token = None
                job.leased_until = None
                job.updated_at = datetime.now(timezone.utc)
                await db.commit()
            
        except ExtractionJobCancelled:
            await db.rollback()
        except Exception as e:
            await db.rollback()
            print(f"❌ Re-extraction job {job_id} failed: {type(e).__name__}")
            await job_worker.retry_or_fail(
                job_id,
                type(e).__name__,
                expected_lease_token=lease_token,
            )


# ============================================================================
# OCR EXTRACTION ENDPOINT
# ============================================================================

class OCRExtractionResponse(BaseModel):
    """Response from OCR extraction."""
    success: bool
    recipe: Optional[dict] = None
    error: Optional[str] = None
    model_used: Optional[str] = None
    latency_seconds: Optional[float] = None


@router.post("/extract/ocr", response_model=OCRExtractionResponse)
async def extract_recipe_from_image(
    image: UploadFile = File(..., description="Image file of a recipe (handwritten or printed)"),
    location: str = Form(default="Guam", description="Location for cost estimation"),
    user: ClerkUser = Depends(get_current_user),
):
    """
    Extract recipe from an uploaded image using AI vision models.
    
    Supports:
    - Handwritten recipe cards
    - Printed recipes
    - Recipe book pages
    - Screenshots of recipes
    
    Uses the pinned routine multimodal model with deterministic fallback.
    """
    if len(location) > 100:
        raise HTTPException(status_code=422, detail="Location is too long")

    print("📸 OCR extraction request received")
    print(f"📍 Location: {location}")
    print(f"📁 File: {image.filename}, Content-Type: {image.content_type}")
    
    try:
        image_bytes = await image.read()
        validated = validate_image_bytes(
            image_bytes,
            max_bytes=MAX_OCR_IMAGE_BYTES,
            declared_content_type=image.content_type,
        )
        image_base64 = base64.b64encode(validated.data).decode("utf-8")
        print(f"🖼️ Image size: {len(validated.data) // 1024}KB")
    except ImageValidationError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail="Failed to read image",
        ) from e
    
    # Extract recipe using vision models
    result = await llm_service.extract_from_image(
        image_base64=image_base64,
        location=location
    )
    
    if result.success:
        print(f"✅ OCR extraction successful: {result.recipe.get('title', 'Untitled')}")
        return OCRExtractionResponse(
            success=True,
            recipe=result.recipe,
            model_used=result.model_used,
            latency_seconds=result.latency_seconds
        )
    else:
        print(f"❌ OCR extraction failed: {result.error}")
        return OCRExtractionResponse(
            success=False,
            error=result.error,
            model_used=result.model_used,
            latency_seconds=result.latency_seconds
        )


@router.post("/extract/ocr/multi", response_model=OCRExtractionResponse)
async def extract_recipe_from_multiple_images(
    images: list[UploadFile] = File(..., description="Multiple image files of a recipe"),
    location: str = Form(default="Guam", description="Location for cost estimation"),
    user: ClerkUser = Depends(get_current_user),
):
    """
    Extract recipe from multiple uploaded images using AI vision models.
    
    Use this for:
    - Multi-page cookbook recipes
    - Front and back of recipe cards
    - Recipes with separate ingredients/instructions pages
    
    All images are analyzed together to extract ONE complete recipe.
    """
    if len(location) > 100:
        raise HTTPException(status_code=422, detail="Location is too long")

    print("📸 Multi-image OCR extraction request received")
    print(f"📍 Location: {location}")
    print(f"🖼️ Number of images: {len(images)}")
    
    if len(images) < 1:
        raise HTTPException(status_code=400, detail="At least one image is required")
    
    if len(images) > 10:
        raise HTTPException(status_code=400, detail="Maximum 10 images allowed")
    
    images_base64 = []
    total_size = 0
    
    for i, image in enumerate(images):
        print(f"   Image {i+1}: {image.filename}, {image.content_type}")
        
        try:
            image_bytes = await image.read()
            validated = validate_image_bytes(
                image_bytes,
                max_bytes=MAX_OCR_IMAGE_BYTES,
                declared_content_type=image.content_type,
            )
            total_size += len(validated.data)
            image_base64 = base64.b64encode(validated.data).decode("utf-8")
            images_base64.append(image_base64)
        except ImageValidationError as e:
            raise HTTPException(status_code=422, detail=f"Image {i + 1}: {e}") from e
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(
                status_code=400,
                detail=f"Failed to read image {i + 1}",
            ) from e
    
    print(f"📦 Total size: {total_size // 1024}KB across {len(images)} images")
    
    # Check total size limit (50MB total)
    if total_size > MAX_OCR_TOTAL_BYTES:
        raise HTTPException(
            status_code=422,
            detail="Total image size too large. Maximum combined size is 40MB."
        )
    
    # Extract recipe using multi-image vision
    result = await llm_service.extract_from_images(
        images_base64=images_base64,
        location=location
    )
    
    if result.success:
        print(f"✅ Multi-image OCR successful: {result.recipe.get('title', 'Untitled')}")
        return OCRExtractionResponse(
            success=True,
            recipe=result.recipe,
            model_used=result.model_used,
            latency_seconds=result.latency_seconds
        )
    else:
        print(f"❌ Multi-image OCR failed: {result.error}")
        return OCRExtractionResponse(
            success=False,
            error=result.error,
            model_used=result.model_used,
            latency_seconds=result.latency_seconds
        )
