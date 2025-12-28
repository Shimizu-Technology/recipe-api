"""Recipe extraction API endpoints."""

import base64
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, UploadFile, File, Form
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, or_
from pydantic import BaseModel, HttpUrl
from typing import Optional
from uuid import UUID, uuid4
from datetime import datetime

from app.db import get_db
from app.models.recipe import Recipe, ExtractionJob, RecipeVersion
from app.services import recipe_extractor, video_service, storage_service
from app.services.llm_client import llm_service
from sqlalchemy import func
from sqlalchemy.orm.attributes import flag_modified


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
from app.services.extractor import ExtractionProgress
from app.auth import get_current_user, ClerkUser

router = APIRouter(prefix="/api", tags=["extraction"])


# Request/Response models
class ExtractRequest(BaseModel):
    """Request to extract a recipe from URL."""
    url: str
    location: str = "Guam"
    notes: str = ""
    quick_check: bool = False  # If true, only check for existing
    is_public: bool = True  # Public by default - shared to library


class ExtractResponse(BaseModel):
    """Response from extraction."""
    id: UUID
    recipe: dict
    is_existing: bool = False


class JobStatusResponse(BaseModel):
    """Status of an extraction job."""
    id: UUID
    url: str
    status: str  # processing|completed|failed
    progress: int
    current_step: str
    message: str
    recipe_id: Optional[UUID] = None
    error_message: Optional[str] = None
    low_confidence: bool = False  # True if extraction quality is uncertain
    confidence_warning: Optional[str] = None  # Warning message for user


# In-memory job storage (for simple implementation)
# In production, this would use Redis or the database
_jobs: dict[str, dict] = {}


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
                status_code=500,
                detail=extraction_result.error or "We couldn't extract a recipe from this website."
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
        is_public=request.is_public,  # Public by default, user can opt out
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
    background_tasks: BackgroundTasks,
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
    
    # Normalize the URL (resolve TikTok short URLs, etc.)
    url = await VideoService.normalize_url(original_url)
    print(f"📎 Normalized URL: {original_url} → {url}")
    
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
    
    # Check for existing extraction job for this URL
    job_result = await db.execute(
        select(ExtractionJob).where(
            or_(ExtractionJob.url == original_url, ExtractionJob.url == url)
        )
    )
    existing_job = job_result.scalar_one_or_none()
    
    if existing_job:
        # If job is still processing, return it
        if existing_job.status == "processing":
            return {
                "job_id": str(existing_job.id),
                "status": "processing",
                "message": "Extraction already in progress"
            }
        
        # If job is completed but no recipe found (shouldn't happen), or failed, 
        # delete the old job and create a new one
        print(f"🗑️ Cleaning up old job {existing_job.id} (status: {existing_job.status})")
        await db.delete(existing_job)
        await db.commit()
    
    # Create new job record
    job_id = str(uuid4())
    
    # Store job in database
    job = ExtractionJob(
        id=job_id,
        url=url,
        location=request.location,
        notes=request.notes,
        status="processing",
        progress=0,
        current_step="initializing",
        message="Starting extraction..."
    )
    
    db.add(job)
    await db.commit()
    
    # Start background task WITH USER ID and display name
    background_tasks.add_task(
        run_extraction_job,
        job_id=job_id,
        url=url,
        location=request.location,
        notes=request.notes,
        user_id=user.id,  # Pass user ID to background task
        user_display_name=user.display_name,  # Pass display name for attribution
        is_public=request.is_public  # Pass public setting
    )
    
    return {
        "job_id": job_id,
        "status": "processing",
        "message": "Extraction started"
    }


async def run_extraction_job(
    job_id: str,
    url: str,
    location: str,
    notes: str,
    user_id: str,  # User ID for the recipe
    user_display_name: str = "A chef",  # Display name for attribution
    is_public: bool = True  # Public by default
):
    """Background task to run extraction."""
    from app.db.database import AsyncSessionLocal
    
    async with AsyncSessionLocal() as db:
        try:
            # Update progress callback
            async def update_progress(progress):
                job_result = await db.execute(
                    select(ExtractionJob).where(ExtractionJob.id == job_id)
                )
                job = job_result.scalar_one_or_none()
                if job:
                    job.progress = progress.progress
                    job.current_step = progress.step
                    job.message = progress.message
                    job.updated_at = datetime.utcnow()
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
                select(ExtractionJob).where(ExtractionJob.id == job_id)
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
                    .where(ExtractionJob.id == job_id)
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
                    is_public=is_public,  # Public by default, user can opt out
                )
                db.add(new_recipe)
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
                    await db.commit()
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
                
                job.status = "completed"
                job.progress = 100
                job.current_step = "complete"
                job.message = completion_msg
                job.recipe_id = new_recipe.id
                job.completed_at = datetime.utcnow()
                job.low_confidence = result.low_confidence
                job.confidence_warning = result.confidence_warning
            else:
                # Update job as failed
                job.status = "failed"
                job.current_step = "error"
                # Use friendly error if available, otherwise raw error
                friendly_msg = result.friendly_error or result.error or "Extraction failed"
                job.message = friendly_msg
                job.error_message = friendly_msg  # Show friendly message to user
            
            job.updated_at = datetime.utcnow()
            await db.commit()
            
        except Exception as e:
            print(f"❌ Extraction job {job_id} failed: {e}")
            # Update job as failed
            job_result = await db.execute(
                select(ExtractionJob).where(ExtractionJob.id == job_id)
            )
            job = job_result.scalar_one_or_none()
            if job:
                job.status = "failed"
                job.error_message = str(e)
                job.message = f"Error: {str(e)}"
                job.updated_at = datetime.utcnow()
                await db.commit()


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
    
    if not job:
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
        select(ExtractionJob).where(ExtractionJob.id == job_id)
    )
    job = result.scalar_one_or_none()
    
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    
    # Only allow cancellation of processing jobs
    if job.status not in ["processing", "pending"]:
        raise HTTPException(
            status_code=400, 
            detail=f"Cannot cancel job with status '{job.status}'"
        )
    
    # Mark as cancelled
    job.status = "cancelled"
    job.current_step = "cancelled"
    job.message = "Extraction cancelled by user"
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
    location: str = "Guam"


@router.post("/re-extract/{recipe_id}/async")
async def start_re_extraction_job(
    recipe_id: UUID,
    request: ReExtractAsyncRequest,
    background_tasks: BackgroundTasks,
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
    
    # Check for existing re-extraction job for this recipe
    job_result = await db.execute(
        select(ExtractionJob).where(
            ExtractionJob.url == f"re-extract:{recipe_id}"
        )
    )
    existing_job = job_result.scalar_one_or_none()
    
    if existing_job:
        if existing_job.status == "processing":
            return {
                "job_id": str(existing_job.id),
                "status": "processing",
                "message": "Re-extraction already in progress"
            }
        # Clean up old job
        await db.delete(existing_job)
        await db.commit()
    
    # Create new job record
    job_id = str(uuid4())
    
    job = ExtractionJob(
        id=job_id,
        url=f"re-extract:{recipe_id}",  # Special URL format for re-extraction
        location=request.location,
        notes="",
        status="processing",
        progress=0,
        current_step="initializing",
        message="Starting re-extraction..."
    )
    
    db.add(job)
    await db.commit()
    
    # Start background task
    background_tasks.add_task(
        run_re_extraction_job,
        job_id=job_id,
        recipe_id=str(recipe_id),
        source_url=recipe.source_url,
        location=request.location,
        user_id=user.id,
    )
    
    return {
        "job_id": job_id,
        "status": "processing",
        "message": "Re-extraction started",
        "recipe_id": str(recipe_id)
    }


async def run_re_extraction_job(
    job_id: str,
    recipe_id: str,
    source_url: str,
    location: str,
    user_id: str,
):
    """Background task to run re-extraction and update existing recipe."""
    from app.db.database import AsyncSessionLocal
    
    async with AsyncSessionLocal() as db:
        try:
            # Update progress callback
            async def update_progress(progress):
                job_result = await db.execute(
                    select(ExtractionJob).where(ExtractionJob.id == job_id)
                )
                job = job_result.scalar_one_or_none()
                if job:
                    job.progress = progress.progress
                    job.current_step = progress.step
                    job.message = progress.message
                    job.updated_at = datetime.utcnow()
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
                select(ExtractionJob).where(ExtractionJob.id == job_id)
            )
            job = job_result.scalar_one_or_none()
            
            if not job:
                print(f"❌ Re-extraction job {job_id} not found")
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
                
                # SINGLE COMMIT for recipe + version together
                await db.commit()
                print(f"🟣 After SINGLE commit, lowConfidence = {recipe.extracted.get('lowConfidence')}")
                
                # Update job as completed
                # Set completion message based on confidence
                if result.low_confidence:
                    completion_msg = "Recipe re-extracted - please review for accuracy"
                else:
                    completion_msg = "Recipe re-extracted successfully!"
                
                await update_progress(ExtractionProgress(
                    step="complete",
                    progress=100,
                    message=completion_msg
                ))
                
                job.status = "completed"
                job.progress = 100
                job.current_step = "complete"
                job.message = completion_msg
                job.recipe_id = recipe.id
                job.completed_at = datetime.utcnow()
                job.low_confidence = result.low_confidence
                job.confidence_warning = result.confidence_warning
            else:
                # Update job as failed
                job.status = "failed"
                job.current_step = "error"
                # Use friendly error if available, otherwise raw error
                friendly_msg = result.friendly_error or result.error or "Re-extraction failed"
                job.message = friendly_msg
                job.error_message = friendly_msg  # Show friendly message to user
            
            job.updated_at = datetime.utcnow()
            await db.commit()
            
        except Exception as e:
            print(f"❌ Re-extraction job {job_id} failed: {e}")
            job_result = await db.execute(
                select(ExtractionJob).where(ExtractionJob.id == job_id)
            )
            job = job_result.scalar_one_or_none()
            if job:
                job.status = "failed"
                job.error_message = str(e)
                job.message = f"Error: {str(e)}"
                job.updated_at = datetime.utcnow()
                await db.commit()


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
):
    """
    Extract recipe from an uploaded image using AI vision models.
    
    Supports:
    - Handwritten recipe cards
    - Printed recipes
    - Recipe book pages
    - Screenshots of recipes
    
    Uses Gemini 2.0 Flash Vision (primary) with GPT-4o Vision fallback.
    """
    print(f"📸 OCR extraction request received")
    print(f"📍 Location: {location}")
    print(f"📁 File: {image.filename}, Content-Type: {image.content_type}")
    
    # Validate file type
    allowed_types = ["image/jpeg", "image/png", "image/gif", "image/webp", "image/jpg"]
    if image.content_type not in allowed_types:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid file type. Allowed types: {', '.join(allowed_types)}"
        )
    
    # Read and encode image
    try:
        image_bytes = await image.read()
        if len(image_bytes) > 20 * 1024 * 1024:  # 20MB limit
            raise HTTPException(
                status_code=400,
                detail="Image file too large. Maximum size is 20MB."
            )
        
        image_base64 = base64.b64encode(image_bytes).decode("utf-8")
        print(f"🖼️ Image size: {len(image_bytes) // 1024}KB")
        
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Failed to read image: {str(e)}"
        )
    
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
):
    """
    Extract recipe from multiple uploaded images using AI vision models.
    
    Use this for:
    - Multi-page cookbook recipes
    - Front and back of recipe cards
    - Recipes with separate ingredients/instructions pages
    
    All images are analyzed together to extract ONE complete recipe.
    """
    print(f"📸 Multi-image OCR extraction request received")
    print(f"📍 Location: {location}")
    print(f"🖼️ Number of images: {len(images)}")
    
    if len(images) < 1:
        raise HTTPException(status_code=400, detail="At least one image is required")
    
    if len(images) > 10:
        raise HTTPException(status_code=400, detail="Maximum 10 images allowed")
    
    allowed_types = ["image/jpeg", "image/png", "image/gif", "image/webp", "image/jpg"]
    images_base64 = []
    total_size = 0
    
    for i, image in enumerate(images):
        print(f"   Image {i+1}: {image.filename}, {image.content_type}")
        
        # Validate file type
        if image.content_type not in allowed_types:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid file type for image {i+1}. Allowed types: {', '.join(allowed_types)}"
            )
        
        # Read and encode image
        try:
            image_bytes = await image.read()
            size_kb = len(image_bytes) // 1024
            total_size += size_kb
            
            if len(image_bytes) > 20 * 1024 * 1024:  # 20MB per image
                raise HTTPException(
                    status_code=400,
                    detail=f"Image {i+1} is too large. Maximum size is 20MB per image."
                )
            
            image_base64 = base64.b64encode(image_bytes).decode("utf-8")
            images_base64.append(image_base64)
            
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(
                status_code=400,
                detail=f"Failed to read image {i+1}: {str(e)}"
            )
    
    print(f"📦 Total size: {total_size}KB across {len(images)} images")
    
    # Check total size limit (50MB total)
    if total_size > 50 * 1024:
        raise HTTPException(
            status_code=400,
            detail="Total image size too large. Maximum combined size is 50MB."
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
