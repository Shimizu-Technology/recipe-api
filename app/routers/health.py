"""Health check endpoint."""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

from app.db import get_db
from app.config import get_settings
from app.models.schemas import HealthResponse

router = APIRouter(tags=["health"])
settings = get_settings()


@router.get("/health", response_model=HealthResponse)
async def health_check(db: AsyncSession = Depends(get_db)):
    """
    Health check endpoint.
    
    Verifies:
    - API is running
    - Database connection is working
    """
    # Test database connection
    try:
        await db.execute(text("SELECT 1"))
        db_status = "connected"
    except Exception as e:
        db_status = f"error: {str(e)}"
    
    return HealthResponse(
        status="healthy" if db_status == "connected" else "unhealthy",
        environment=settings.environment,
        database=db_status,
    )


@router.get("/sentry-debug")
async def trigger_error():
    """
    Test endpoint to verify Sentry is working.
    Triggers a division by zero error that gets captured by Sentry.
    
    Only use for testing - will throw an error!
    """
    division_by_zero = 1 / 0
    return {"this": "won't be reached"}

