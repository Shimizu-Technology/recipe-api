"""Pydantic schemas for API request/response validation.

These schemas match the TypeScript types from the Next.js app:
- RecipeJSON
- RecipeComponent
- Ingredient
- etc.
"""

from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime
from uuid import UUID


# ============================================================
# Nested Types (matching TypeScript interfaces)
# ============================================================

class Ingredient(BaseModel):
    """Single ingredient with optional cost estimate."""
    quantity: Optional[str] = None
    unit: Optional[str] = None
    name: str
    notes: Optional[str] = None
    estimatedCost: Optional[float] = None


class RecipeComponent(BaseModel):
    """A component of a recipe (e.g., 'Main Dish', 'Sauce', 'Side')."""
    name: str
    ingredients: list[Ingredient]
    steps: list[str]
    notes: Optional[str] = None


class Times(BaseModel):
    """Cooking time breakdown."""
    prep: Optional[str] = None
    cook: Optional[str] = None
    total: Optional[str] = None


class NutritionValues(BaseModel):
    """Nutritional values (used for both per-serving and total)."""
    calories: Optional[int] = None
    protein: Optional[float] = None  # grams
    carbs: Optional[float] = None  # grams
    fat: Optional[float] = None  # grams
    fiber: Optional[float] = None  # grams
    sugar: Optional[float] = None  # grams
    sodium: Optional[float] = None  # milligrams


class Nutrition(BaseModel):
    """Complete nutrition information."""
    perServing: NutritionValues
    total: NutritionValues


class Media(BaseModel):
    """Media attachments for recipe."""
    thumbnail: Optional[str] = None


# ============================================================
# Main Recipe Schema (the JSONB 'extracted' field)
# ============================================================

class RecipeExtracted(BaseModel):
    """
    The full extracted recipe data.
    This is what gets stored in the 'extracted' JSONB column.
    """
    title: str
    sourceUrl: str
    servings: Optional[int] = None
    times: Optional[Times] = None
    # New component-based structure
    components: list[RecipeComponent] = []
    # Legacy fields (kept for backward compatibility)
    ingredients: list[Ingredient] = []
    steps: list[str] = []
    equipment: Optional[list[str]] = None
    notes: Optional[str] = None
    tags: list[str] = []
    media: Optional[Media] = None
    totalEstimatedCost: Optional[float] = None
    costLocation: str = "US Average"
    nutrition: Nutrition


# ============================================================
# API Response Schemas
# ============================================================

class RecipeResponse(BaseModel):
    """Full recipe response for API."""
    id: UUID
    source_url: str
    source_type: str
    raw_text: Optional[str] = None
    extracted: RecipeExtracted
    thumbnail_url: Optional[str] = None
    extraction_method: Optional[str] = None
    extraction_quality: Optional[str] = None
    has_audio_transcript: bool = False
    created_at: datetime
    user_id: Optional[str] = None
    is_public: bool = False
    
    class Config:
        from_attributes = True


class RecipeListItem(BaseModel):
    """Simplified recipe for list views."""
    id: UUID
    title: str
    source_url: str
    source_type: str
    thumbnail_url: Optional[str] = None
    extraction_quality: Optional[str] = None
    has_audio_transcript: bool = False
    tags: list[str] = []
    servings: Optional[int] = None
    total_time: Optional[str] = None
    created_at: datetime
    user_id: Optional[str] = None
    is_public: bool = False
    
    class Config:
        from_attributes = True


class RecipeSearchResult(BaseModel):
    """Recipe search result with relevance info."""
    recipe: RecipeListItem
    match_type: str = "title"  # title|ingredient|tag


# ============================================================
# Extraction Job Schemas
# ============================================================

class ExtractionJobCreate(BaseModel):
    """Request to start a new extraction."""
    url: str
    location: str = "Guam"
    notes: str = ""


class ExtractionJobResponse(BaseModel):
    """Extraction job status response."""
    id: UUID
    url: str
    location: str
    notes: str
    status: str  # processing|completed|failed
    progress: int  # 0-100
    current_step: str
    message: str
    estimated_duration: int
    recipe_id: Optional[UUID] = None
    error_message: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    completed_at: Optional[datetime] = None
    
    class Config:
        from_attributes = True


# ============================================================
# Utility Schemas
# ============================================================

class HealthResponse(BaseModel):
    """Health check response."""
    status: str = "healthy"
    environment: str
    database: str = "connected"


class ErrorResponse(BaseModel):
    """Standard error response."""
    detail: str
    error_code: Optional[str] = None


class PaginatedResponse(BaseModel):
    """Generic paginated response wrapper."""
    items: list
    total: int
    page: int
    per_page: int
    has_more: bool

