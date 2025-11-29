"""Recipe extraction API endpoints."""

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel, HttpUrl
from typing import Optional
from uuid import UUID, uuid4
from datetime import datetime

from app.db import get_db
from app.models.recipe import Recipe, ExtractionJob
from app.services import recipe_extractor, video_service, storage_service
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
        raise HTTPException(
            status_code=400,
            detail="Unsupported URL. Please provide a TikTok, YouTube, or Instagram video URL."
        )
    
    # Run extraction
    extraction_result = await recipe_extractor.extract(
        url=url,
        location=request.location,
        notes=request.notes
    )
    
    if not extraction_result.success:
        raise HTTPException(
            status_code=500,
            detail=f"Extraction failed: {extraction_result.error}"
        )
    
    # Save to database with user_id
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
        return {
            "job_id": None,
            "status": "completed",
            "recipe_id": str(existing.id),
            "is_existing": True
        }
    
    # Create job record
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
    
    # Start background task WITH USER ID
    background_tasks.add_task(
        run_extraction_job,
        job_id=job_id,
        url=url,
        location=request.location,
        notes=request.notes,
        user_id=user.id,  # Pass user ID to background task
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
            
            # Run extraction
            platform = video_service.detect_platform(url)
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
            
            if result.success:
                # Save recipe WITH USER ID
                new_recipe = Recipe(
                    source_url=url,
                    source_type=platform,
                    raw_text=result.raw_text,
                    extracted=result.recipe,
                    thumbnail_url=result.thumbnail_url,
                    extraction_method=result.extraction_method,
                    extraction_quality=result.extraction_quality,
                    has_audio_transcript=result.has_audio_transcript,
                    user_id=user_id,  # Assign to user
                    is_public=is_public,  # Public by default, user can opt out
                )
                db.add(new_recipe)
                await db.commit()
                await db.refresh(new_recipe)
                
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
                        # Update recipe with S3 URL
                        new_recipe.thumbnail_url = s3_url
                        if new_recipe.extracted and "media" in new_recipe.extracted:
                            new_recipe.extracted["media"]["thumbnail"] = s3_url
                        await db.commit()
                
                # Update job as completed (only NOW, after everything is done)
                await update_progress(ExtractionProgress(
                    step="complete",
                    progress=100,
                    message="Recipe extracted successfully!"
                ))
                
                job.status = "completed"
                job.progress = 100
                job.current_step = "complete"
                job.message = "Recipe extracted successfully!"
                job.recipe_id = new_recipe.id
                job.completed_at = datetime.utcnow()
            else:
                # Update job as failed
                job.status = "failed"
                job.current_step = "error"
                job.message = f"Extraction failed: {result.error}"
                job.error_message = result.error
            
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
        error_message=job.error_message
    )


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
