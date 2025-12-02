"""Recipe Extractor API - FastAPI Application."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.routers import recipes_router, health_router, extract_router, grocery_router, chat_router, users_router, collections_router

settings = get_settings()

# Create FastAPI app
app = FastAPI(
    title=settings.api_title,
    version=settings.api_version,
    description="Transform cooking videos into structured recipes with AI",
    docs_url="/docs",
    redoc_url="/redoc",
)

# CORS middleware - allow React Native and web clients
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",      # Next.js dev
        "http://localhost:8081",      # Expo dev
        "http://localhost:19006",     # Expo web
        "exp://localhost:8081",       # Expo Go
        "*",                          # Allow all for development (restrict in prod)
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(health_router)
app.include_router(recipes_router)
app.include_router(extract_router)
app.include_router(grocery_router)
app.include_router(chat_router)
app.include_router(users_router)
app.include_router(collections_router)


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
    print(f"📚 Docs: http://localhost:8000/docs")


@app.on_event("shutdown")
async def shutdown():
    """Run on application shutdown."""
    print("👋 Shutting down Recipe Extractor API")
