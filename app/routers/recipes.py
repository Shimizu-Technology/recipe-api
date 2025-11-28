"""Recipe API endpoints - CRUD operations."""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, or_, delete
from pydantic import BaseModel
from uuid import UUID
from typing import Optional

from app.db import get_db
from app.models.recipe import Recipe
from app.models.schemas import RecipeResponse, RecipeListItem

router = APIRouter(prefix="/api/recipes", tags=["recipes"])


class RecipeUpdate(BaseModel):
    """Request to update a recipe."""
    title: Optional[str] = None
    servings: Optional[int] = None
    notes: Optional[str] = None
    tags: Optional[list[str]] = None


def recipe_to_list_item(recipe: Recipe) -> RecipeListItem:
    """Convert Recipe model to RecipeListItem schema."""
    extracted = recipe.extracted or {}
    times = extracted.get("times", {})
    
    return RecipeListItem(
        id=recipe.id,
        title=extracted.get("title", "Untitled Recipe"),
        source_url=recipe.source_url,
        source_type=recipe.source_type,
        thumbnail_url=recipe.thumbnail_url,
        extraction_quality=recipe.extraction_quality,
        has_audio_transcript=recipe.has_audio_transcript or False,
        tags=extracted.get("tags", []),
        servings=extracted.get("servings"),
        total_time=times.get("total"),
        created_at=recipe.created_at,
    )


@router.get("/", response_model=list[RecipeListItem])
async def get_recipes(
    limit: int = Query(default=50, le=100, description="Max recipes to return"),
    offset: int = Query(default=0, ge=0, description="Number of recipes to skip"),
    db: AsyncSession = Depends(get_db),
):
    """
    Get all recipes, ordered by most recent first.
    
    This reads from your existing Neon database!
    """
    result = await db.execute(
        select(Recipe)
        .order_by(Recipe.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    recipes = result.scalars().all()
    
    return [recipe_to_list_item(r) for r in recipes]


@router.get("/count")
async def get_recipe_count(db: AsyncSession = Depends(get_db)):
    """Get total number of recipes in database."""
    result = await db.execute(select(func.count(Recipe.id)))
    count = result.scalar()
    return {"count": count}


@router.get("/search", response_model=list[RecipeListItem])
async def search_recipes(
    q: str = Query(..., min_length=1, description="Search query"),
    limit: int = Query(default=20, le=50),
    db: AsyncSession = Depends(get_db),
):
    """
    Search recipes by title, ingredients, or tags.
    
    Uses case-insensitive partial matching.
    """
    search_term = f"%{q.lower()}%"
    
    # Search in JSONB fields
    result = await db.execute(
        select(Recipe)
        .where(
            or_(
                # Search in title (JSONB)
                func.lower(Recipe.extracted["title"].astext).like(search_term),
                # Search in tags array (JSONB)
                Recipe.extracted["tags"].astext.ilike(search_term),
                # Search in source URL
                func.lower(Recipe.source_url).like(search_term),
            )
        )
        .order_by(Recipe.created_at.desc())
        .limit(limit)
    )
    recipes = result.scalars().all()
    
    return [recipe_to_list_item(r) for r in recipes]


@router.get("/recent", response_model=list[RecipeListItem])
async def get_recent_recipes(
    limit: int = Query(default=10, le=20),
    db: AsyncSession = Depends(get_db),
):
    """Get most recently created recipes."""
    result = await db.execute(
        select(Recipe)
        .order_by(Recipe.created_at.desc())
        .limit(limit)
    )
    recipes = result.scalars().all()
    
    return [recipe_to_list_item(r) for r in recipes]


@router.get("/check-duplicate")
async def check_duplicate(
    url: str = Query(..., description="Source URL to check"),
    db: AsyncSession = Depends(get_db),
):
    """
    Check if a recipe with this URL already exists.
    
    Returns the existing recipe ID if found.
    """
    result = await db.execute(
        select(Recipe).where(Recipe.source_url == url)
    )
    existing = result.scalar_one_or_none()
    
    if existing:
        return {
            "exists": True,
            "recipe_id": str(existing.id),
            "title": existing.extracted.get("title", "Untitled") if existing.extracted else "Untitled",
        }
    
    return {"exists": False}


@router.get("/{recipe_id}", response_model=RecipeResponse)
async def get_recipe(
    recipe_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """
    Get a single recipe by ID.
    
    Returns full recipe data including all extracted information.
    """
    result = await db.execute(
        select(Recipe).where(Recipe.id == recipe_id)
    )
    recipe = result.scalar_one_or_none()
    
    if not recipe:
        raise HTTPException(status_code=404, detail="Recipe not found")
    
    return recipe


@router.put("/{recipe_id}", response_model=RecipeResponse)
async def update_recipe(
    recipe_id: UUID,
    update: RecipeUpdate,
    db: AsyncSession = Depends(get_db),
):
    """
    Update a recipe's metadata.
    
    Only updates provided fields; others remain unchanged.
    """
    result = await db.execute(
        select(Recipe).where(Recipe.id == recipe_id)
    )
    recipe = result.scalar_one_or_none()
    
    if not recipe:
        raise HTTPException(status_code=404, detail="Recipe not found")
    
    # Update the extracted JSONB with new values
    extracted = dict(recipe.extracted) if recipe.extracted else {}
    
    if update.title is not None:
        extracted["title"] = update.title
    if update.servings is not None:
        extracted["servings"] = update.servings
    if update.notes is not None:
        extracted["notes"] = update.notes
    if update.tags is not None:
        extracted["tags"] = update.tags
    
    recipe.extracted = extracted
    
    await db.commit()
    await db.refresh(recipe)
    
    return recipe


@router.delete("/{recipe_id}")
async def delete_recipe(
    recipe_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """
    Delete a recipe by ID.
    
    This permanently removes the recipe from the database.
    """
    result = await db.execute(
        select(Recipe).where(Recipe.id == recipe_id)
    )
    recipe = result.scalar_one_or_none()
    
    if not recipe:
        raise HTTPException(status_code=404, detail="Recipe not found")
    
    await db.delete(recipe)
    await db.commit()
    
    return {"message": "Recipe deleted successfully", "id": str(recipe_id)}
