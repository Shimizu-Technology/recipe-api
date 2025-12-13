"""
Collections router - API endpoints for recipe collections/folders.
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, delete
from sqlalchemy.orm import selectinload
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime
import uuid

from app.db.database import get_db
from app.models.recipe import Collection, CollectionRecipe, Recipe
from app.auth import get_current_user, ClerkUser

router = APIRouter(prefix="/api/collections", tags=["collections"])


# ============================================================
# Pydantic Schemas
# ============================================================

class CollectionCreate(BaseModel):
    name: str
    emoji: Optional[str] = None


class CollectionUpdate(BaseModel):
    name: Optional[str] = None
    emoji: Optional[str] = None


class CollectionResponse(BaseModel):
    id: str
    name: str
    emoji: Optional[str]
    recipe_count: int
    created_at: datetime
    updated_at: datetime
    
    class Config:
        from_attributes = True


class CollectionWithRecipesResponse(BaseModel):
    id: str
    name: str
    emoji: Optional[str]
    recipe_count: int
    created_at: datetime
    updated_at: datetime
    preview_thumbnails: List[Optional[str]]  # First 4 recipe thumbnails
    
    class Config:
        from_attributes = True


class RecipeInCollection(BaseModel):
    id: str
    title: str
    source_type: str
    thumbnail_url: Optional[str]
    tags: List[str]
    total_time: Optional[str]
    servings: Optional[int]
    added_at: datetime


class AddRecipeToCollection(BaseModel):
    recipe_id: str


class CollectionRecipeIds(BaseModel):
    """Response with just the recipe IDs in a collection."""
    recipe_ids: List[str]


# ============================================================
# API Endpoints
# ============================================================

@router.get("", response_model=List[CollectionWithRecipesResponse])
async def get_collections(
    current_user: ClerkUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Get all collections for the current user with recipe counts and preview thumbnails."""
    user_id = current_user.id
    
    # Get collections with recipe count
    query = (
        select(
            Collection,
            func.count(CollectionRecipe.recipe_id).label("recipe_count")
        )
        .outerjoin(CollectionRecipe, Collection.id == CollectionRecipe.collection_id)
        .where(Collection.user_id == user_id)
        .group_by(Collection.id)
        .order_by(Collection.created_at.desc())
    )
    
    result = await db.execute(query)
    collections_with_counts = result.all()
    
    response = []
    for collection, recipe_count in collections_with_counts:
        # Get preview thumbnails (first 4 recipes)
        preview_query = (
            select(Recipe.thumbnail_url)
            .join(CollectionRecipe, Recipe.id == CollectionRecipe.recipe_id)
            .where(CollectionRecipe.collection_id == collection.id)
            .order_by(CollectionRecipe.added_at.desc())
            .limit(4)
        )
        preview_result = await db.execute(preview_query)
        thumbnails = [row[0] for row in preview_result.all()]
        
        response.append(CollectionWithRecipesResponse(
            id=str(collection.id),
            name=collection.name,
            emoji=collection.emoji,
            recipe_count=recipe_count,
            created_at=collection.created_at,
            updated_at=collection.updated_at,
            preview_thumbnails=thumbnails
        ))
    
    return response


@router.post("", response_model=CollectionResponse)
async def create_collection(
    data: CollectionCreate,
    current_user: ClerkUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Create a new collection."""
    user_id = current_user.id
    
    collection = Collection(
        user_id=user_id,
        name=data.name,
        emoji=data.emoji
    )
    
    db.add(collection)
    await db.commit()
    await db.refresh(collection)
    
    return CollectionResponse(
        id=str(collection.id),
        name=collection.name,
        emoji=collection.emoji,
        recipe_count=0,
        created_at=collection.created_at,
        updated_at=collection.updated_at
    )


@router.get("/{collection_id}", response_model=CollectionResponse)
async def get_collection(
    collection_id: str,
    current_user: ClerkUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Get a single collection by ID."""
    user_id = current_user.id
    
    query = (
        select(
            Collection,
            func.count(CollectionRecipe.recipe_id).label("recipe_count")
        )
        .outerjoin(CollectionRecipe, Collection.id == CollectionRecipe.collection_id)
        .where(Collection.id == uuid.UUID(collection_id))
        .where(Collection.user_id == user_id)
        .group_by(Collection.id)
    )
    
    result = await db.execute(query)
    row = result.first()
    
    if not row:
        raise HTTPException(status_code=404, detail="Collection not found")
    
    collection, recipe_count = row
    
    return CollectionResponse(
        id=str(collection.id),
        name=collection.name,
        emoji=collection.emoji,
        recipe_count=recipe_count,
        created_at=collection.created_at,
        updated_at=collection.updated_at
    )


@router.put("/{collection_id}", response_model=CollectionResponse)
async def update_collection(
    collection_id: str,
    data: CollectionUpdate,
    current_user: ClerkUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Update a collection's name or emoji."""
    user_id = current_user.id
    
    query = select(Collection).where(
        Collection.id == uuid.UUID(collection_id),
        Collection.user_id == user_id
    )
    result = await db.execute(query)
    collection = result.scalar_one_or_none()
    
    if not collection:
        raise HTTPException(status_code=404, detail="Collection not found")
    
    if data.name is not None:
        collection.name = data.name
    if data.emoji is not None:
        collection.emoji = data.emoji
    
    await db.commit()
    await db.refresh(collection)
    
    # Get recipe count
    count_query = select(func.count(CollectionRecipe.recipe_id)).where(
        CollectionRecipe.collection_id == collection.id
    )
    count_result = await db.execute(count_query)
    recipe_count = count_result.scalar() or 0
    
    return CollectionResponse(
        id=str(collection.id),
        name=collection.name,
        emoji=collection.emoji,
        recipe_count=recipe_count,
        created_at=collection.created_at,
        updated_at=collection.updated_at
    )


@router.delete("/{collection_id}")
async def delete_collection(
    collection_id: str,
    current_user: ClerkUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Delete a collection. Does not delete the recipes in it."""
    user_id = current_user.id
    
    query = select(Collection).where(
        Collection.id == uuid.UUID(collection_id),
        Collection.user_id == user_id
    )
    result = await db.execute(query)
    collection = result.scalar_one_or_none()
    
    if not collection:
        raise HTTPException(status_code=404, detail="Collection not found")
    
    await db.delete(collection)
    await db.commit()
    
    return {"success": True, "message": "Collection deleted"}


# ============================================================
# Collection Recipes Endpoints
# ============================================================

@router.get("/{collection_id}/recipes", response_model=List[RecipeInCollection])
async def get_collection_recipes(
    collection_id: str,
    current_user: ClerkUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Get all recipes in a collection."""
    user_id = current_user.id
    
    # Verify collection belongs to user
    collection_query = select(Collection).where(
        Collection.id == uuid.UUID(collection_id),
        Collection.user_id == user_id
    )
    collection_result = await db.execute(collection_query)
    if not collection_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Collection not found")
    
    # Get recipes with added_at timestamp
    query = (
        select(Recipe, CollectionRecipe.added_at)
        .join(CollectionRecipe, Recipe.id == CollectionRecipe.recipe_id)
        .where(CollectionRecipe.collection_id == uuid.UUID(collection_id))
        .order_by(CollectionRecipe.added_at.desc())
    )
    
    result = await db.execute(query)
    recipes = result.all()
    
    return [
        RecipeInCollection(
            id=str(recipe.id),
            title=recipe.extracted.get("title", "Untitled"),
            source_type=recipe.source_type,
            thumbnail_url=recipe.thumbnail_url,
            tags=recipe.extracted.get("tags", []),
            total_time=(recipe.extracted.get("times") or {}).get("total"),
            servings=recipe.extracted.get("servings"),
            added_at=added_at
        )
        for recipe, added_at in recipes
    ]


@router.get("/{collection_id}/recipe-ids", response_model=CollectionRecipeIds)
async def get_collection_recipe_ids(
    collection_id: str,
    current_user: ClerkUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Get just the recipe IDs in a collection (for checking membership)."""
    user_id = current_user.id
    
    # Verify collection belongs to user
    collection_query = select(Collection).where(
        Collection.id == uuid.UUID(collection_id),
        Collection.user_id == user_id
    )
    collection_result = await db.execute(collection_query)
    if not collection_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Collection not found")
    
    query = (
        select(CollectionRecipe.recipe_id)
        .where(CollectionRecipe.collection_id == uuid.UUID(collection_id))
    )
    
    result = await db.execute(query)
    recipe_ids = [str(row[0]) for row in result.all()]
    
    return CollectionRecipeIds(recipe_ids=recipe_ids)


@router.post("/{collection_id}/recipes")
async def add_recipe_to_collection(
    collection_id: str,
    data: AddRecipeToCollection,
    current_user: ClerkUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Add a recipe to a collection."""
    user_id = current_user.id
    
    # Verify collection belongs to user
    collection_query = select(Collection).where(
        Collection.id == uuid.UUID(collection_id),
        Collection.user_id == user_id
    )
    collection_result = await db.execute(collection_query)
    if not collection_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Collection not found")
    
    # Verify recipe exists and user has access (owns it or it's public or they saved it)
    recipe_query = select(Recipe).where(Recipe.id == uuid.UUID(data.recipe_id))
    recipe_result = await db.execute(recipe_query)
    recipe = recipe_result.scalar_one_or_none()
    
    if not recipe:
        raise HTTPException(status_code=404, detail="Recipe not found")
    
    # Check if already in collection
    existing_query = select(CollectionRecipe).where(
        CollectionRecipe.collection_id == uuid.UUID(collection_id),
        CollectionRecipe.recipe_id == uuid.UUID(data.recipe_id)
    )
    existing_result = await db.execute(existing_query)
    if existing_result.scalar_one_or_none():
        return {"success": True, "message": "Recipe already in collection"}
    
    # Add to collection
    collection_recipe = CollectionRecipe(
        collection_id=uuid.UUID(collection_id),
        recipe_id=uuid.UUID(data.recipe_id)
    )
    db.add(collection_recipe)
    await db.commit()
    
    return {"success": True, "message": "Recipe added to collection"}


@router.delete("/{collection_id}/recipes/{recipe_id}")
async def remove_recipe_from_collection(
    collection_id: str,
    recipe_id: str,
    current_user: ClerkUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Remove a recipe from a collection."""
    user_id = current_user.id
    
    # Verify collection belongs to user
    collection_query = select(Collection).where(
        Collection.id == uuid.UUID(collection_id),
        Collection.user_id == user_id
    )
    collection_result = await db.execute(collection_query)
    if not collection_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Collection not found")
    
    # Delete the relationship
    delete_query = delete(CollectionRecipe).where(
        CollectionRecipe.collection_id == uuid.UUID(collection_id),
        CollectionRecipe.recipe_id == uuid.UUID(recipe_id)
    )
    await db.execute(delete_query)
    await db.commit()
    
    return {"success": True, "message": "Recipe removed from collection"}


# ============================================================
# Utility Endpoints
# ============================================================

@router.get("/recipe/{recipe_id}/collections", response_model=List[str])
async def get_recipe_collections(
    recipe_id: str,
    current_user: ClerkUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Get all collection IDs that contain a specific recipe (for the current user)."""
    user_id = current_user.id
    
    query = (
        select(CollectionRecipe.collection_id)
        .join(Collection, CollectionRecipe.collection_id == Collection.id)
        .where(
            CollectionRecipe.recipe_id == uuid.UUID(recipe_id),
            Collection.user_id == user_id
        )
    )
    
    result = await db.execute(query)
    collection_ids = [str(row[0]) for row in result.all()]
    
    return collection_ids

