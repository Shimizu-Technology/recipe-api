"""Recipe Extractor API - FastAPI Application."""

import sentry_sdk
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.routers import (
    chat_router,
    collections_router,
    cooking_chat_router,
    extract_router,
    grocery_router,
    health_router,
    meal_plans_router,
    recipes_router,
    tts_router,
    users_router,
)

settings = get_settings()

# Initialize Sentry for error monitoring
if settings.sentry_dsn:
    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.environment,
        # Performance monitoring (20% sample - cost-effective for production)
        traces_sample_rate=0.2,
        # Profiling (10% sample)
        profiles_sample_rate=0.1,
        enable_tracing=True,
        # Don't send PII
        send_default_pii=False,
    )
    print(f"📊 Sentry initialized for {settings.environment}")
else:
    print("📊 Sentry not configured (no SENTRY_DSN)")

# Create FastAPI app
app = FastAPI(
    title=settings.api_title,
    version=settings.api_version,
    description="Transform cooking videos into structured recipes with AI",
    docs_url="/docs",
    redoc_url="/redoc",
)

# CORS middleware - browser callers only. React Native does not require CORS.
allowed_origins = settings.allowed_cors_origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials="*" not in allowed_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(health_router)
app.include_router(recipes_router)
app.include_router(extract_router)
app.include_router(grocery_router)
app.include_router(chat_router)
app.include_router(cooking_chat_router)
app.include_router(users_router)
app.include_router(collections_router)
app.include_router(meal_plans_router)
app.include_router(tts_router)


@app.get("/")
async def root():
    """Root endpoint with API info."""
    return {
        "name": settings.api_title,
        "version": settings.api_version,
        "docs": "/docs",
        "health": "/health",
    }


# Startup/shutdown events
@app.on_event("startup")
async def startup():
    """Run on application startup."""
    print(f"🚀 {settings.api_title} v{settings.api_version}")
    print(f"📍 Environment: {settings.environment}")
    print("📚 Docs: http://localhost:8000/docs")


@app.on_event("shutdown")
async def shutdown():
    """Run on application shutdown."""
    print("👋 Shutting down Recipe Extractor API")
