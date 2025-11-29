"""Recipe API endpoints - CRUD operations with user authentication."""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, or_
from pydantic import BaseModel
from uuid import UUID
from typing import Optional

from app.db import get_db
from app.models.recipe import Recipe
from app.models.schemas import RecipeResponse, RecipeListItem
from app.auth import get_current_user, get_optional_user, ClerkUser

router = APIRouter(prefix="/api/recipes", tags=["recipes"])


class RecipeUpdate(BaseModel):
    """Request to update a recipe."""
    title: Optional[str] = None
    servings: Optional[int] = None
    notes: Optional[str] = None
    tags: Optional[list[str]] = None
    is_public: Optional[bool] = None


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
        is_public=recipe.is_public,
        user_id=recipe.user_id,
    )


@router.get("/", response_model=list[RecipeListItem])
async def get_my_recipes(
    limit: int = Query(default=50, le=100, description="Max recipes to return"),
    offset: int = Query(default=0, ge=0, description="Number of recipes to skip"),
    db: AsyncSession = Depends(get_db),
    user: ClerkUser = Depends(get_current_user),
):
    """
    Get current user's recipes, ordered by most recent first.
    
    Requires authentication.
    """
    result = await db.execute(
        select(Recipe)
        .where(Recipe.user_id == user.id)
        .order_by(Recipe.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    recipes = result.scalars().all()
    
    return [recipe_to_list_item(r) for r in recipes]


@router.get("/discover", response_model=list[RecipeListItem])
async def get_public_recipes(
    limit: int = Query(default=50, le=100, description="Max recipes to return"),
    offset: int = Query(default=0, ge=0, description="Number of recipes to skip"),
    db: AsyncSession = Depends(get_db),
    user: Optional[ClerkUser] = Depends(get_optional_user),
):
    """
    Get all public recipes (the shared library).
    
    Works with or without authentication.
    """
    result = await db.execute(
        select(Recipe)
        .where(Recipe.is_public == True)
        .order_by(Recipe.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    recipes = result.scalars().all()
    
    return [recipe_to_list_item(r) for r in recipes]


@router.get("/count")
async def get_recipe_count(
    db: AsyncSession = Depends(get_db),
    user: ClerkUser = Depends(get_current_user),
):
    """Get total number of user's recipes."""
    result = await db.execute(
        select(func.count(Recipe.id))
        .where(Recipe.user_id == user.id)
    )
    count = result.scalar()
    return {"count": count or 0}


@router.get("/discover/count")
async def get_public_recipe_count(
    db: AsyncSession = Depends(get_db),
):
    """Get total number of public recipes."""
    result = await db.execute(
        select(func.count(Recipe.id))
        .where(Recipe.is_public == True)
    )
    count = result.scalar()
    return {"count": count or 0}


@router.get("/search", response_model=list[RecipeListItem])
async def search_recipes(
    q: str = Query(..., min_length=1, description="Search query"),
    limit: int = Query(default=20, le=50),
    db: AsyncSession = Depends(get_db),
    user: ClerkUser = Depends(get_current_user),
):
    """
    Search user's recipes by title, ingredients, or tags.
    """
    search_term = f"%{q.lower()}%"
    
    result = await db.execute(
        select(Recipe)
        .where(
            Recipe.user_id == user.id,
            or_(
                func.lower(Recipe.extracted["title"].astext).like(search_term),
                Recipe.extracted["tags"].astext.ilike(search_term),
                func.lower(Recipe.source_url).like(search_term),
            )
        )
        .order_by(Recipe.created_at.desc())
        .limit(limit)
    )
    recipes = result.scalars().all()
    
    return [recipe_to_list_item(r) for r in recipes]


@router.get("/discover/search", response_model=list[RecipeListItem])
async def search_public_recipes(
    q: str = Query(..., min_length=1, description="Search query"),
    limit: int = Query(default=20, le=50),
    db: AsyncSession = Depends(get_db),
):
    """
    Search public recipes by title, ingredients, or tags.
    """
    search_term = f"%{q.lower()}%"
    
    result = await db.execute(
        select(Recipe)
        .where(
            Recipe.is_public == True,
            or_(
                func.lower(Recipe.extracted["title"].astext).like(search_term),
                Recipe.extracted["tags"].astext.ilike(search_term),
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
    user: ClerkUser = Depends(get_current_user),
):
    """Get user's most recently created recipes."""
    result = await db.execute(
        select(Recipe)
        .where(Recipe.user_id == user.id)
        .order_by(Recipe.created_at.desc())
        .limit(limit)
    )
    recipes = result.scalars().all()
    
    return [recipe_to_list_item(r) for r in recipes]


@router.get("/check-duplicate")
async def check_duplicate(
    url: str = Query(..., description="Source URL to check"),
    db: AsyncSession = Depends(get_db),
    user: ClerkUser = Depends(get_current_user),
):
    """
    Check if user already has a recipe with this URL.
    """
    result = await db.execute(
        select(Recipe).where(
            Recipe.source_url == url,
            Recipe.user_id == user.id
        )
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
    user: Optional[ClerkUser] = Depends(get_optional_user),
):
    """
    Get a single recipe by ID.
    
    Returns recipe if:
    - It's public, OR
    - It belongs to the current user
    """
    result = await db.execute(
        select(Recipe).where(Recipe.id == recipe_id)
    )
    recipe = result.scalar_one_or_none()
    
    if not recipe:
        raise HTTPException(status_code=404, detail="Recipe not found")
    
    # Check access: public recipes are accessible to anyone
    # Private recipes only accessible to owner
    if not recipe.is_public:
        if not user or recipe.user_id != user.id:
            raise HTTPException(status_code=403, detail="Access denied")
    
    return recipe


@router.put("/{recipe_id}", response_model=RecipeResponse)
async def update_recipe(
    recipe_id: UUID,
    update: RecipeUpdate,
    db: AsyncSession = Depends(get_db),
    user: ClerkUser = Depends(get_current_user),
):
    """
    Update a recipe's metadata.
    
    Only the recipe owner can update it.
    """
    result = await db.execute(
        select(Recipe).where(Recipe.id == recipe_id)
    )
    recipe = result.scalar_one_or_none()
    
    if not recipe:
        raise HTTPException(status_code=404, detail="Recipe not found")
    
    # Only owner can update
    if recipe.user_id != user.id:
        raise HTTPException(status_code=403, detail="You can only update your own recipes")
    
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
    
    # Update is_public if provided
    if update.is_public is not None:
        recipe.is_public = update.is_public
    
    await db.commit()
    await db.refresh(recipe)
    
    return recipe


@router.post("/{recipe_id}/share")
async def toggle_recipe_sharing(
    recipe_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: ClerkUser = Depends(get_current_user),
):
    """
    Toggle whether a recipe is shared to the public library.
    """
    result = await db.execute(
        select(Recipe).where(Recipe.id == recipe_id)
    )
    recipe = result.scalar_one_or_none()
    
    if not recipe:
        raise HTTPException(status_code=404, detail="Recipe not found")
    
    if recipe.user_id != user.id:
        raise HTTPException(status_code=403, detail="You can only share your own recipes")
    
    # Toggle is_public
    recipe.is_public = not recipe.is_public
    await db.commit()
    
    return {
        "is_public": recipe.is_public,
        "message": "Recipe shared to library" if recipe.is_public else "Recipe removed from library"
    }


@router.delete("/{recipe_id}")
async def delete_recipe(
    recipe_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: ClerkUser = Depends(get_current_user),
):
    """
    Delete a recipe by ID.
    
    Only the recipe owner can delete it.
    """
    result = await db.execute(
        select(Recipe).where(Recipe.id == recipe_id)
    )
    recipe = result.scalar_one_or_none()
    
    if not recipe:
        raise HTTPException(status_code=404, detail="Recipe not found")
    
    # Only owner can delete
    if recipe.user_id != user.id:
        raise HTTPException(status_code=403, detail="You can only delete your own recipes")
    
    await db.delete(recipe)
    await db.commit()
    
    return {"message": "Recipe deleted successfully", "id": str(recipe_id)}
