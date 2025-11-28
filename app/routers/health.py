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

