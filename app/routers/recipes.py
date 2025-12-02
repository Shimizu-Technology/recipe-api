"""Recipe API endpoints - CRUD operations with user authentication."""

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File, Form
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, or_, String
from pydantic import BaseModel, ConfigDict
from uuid import UUID
from typing import Optional, List, Generic, TypeVar
import json

from app.db import get_db
from app.models.recipe import Recipe, SavedRecipe
from app.models.schemas import RecipeResponse, RecipeListItem
from app.auth import get_current_user, get_optional_user, ClerkUser
from app.services.storage import storage_service

router = APIRouter(prefix="/api/recipes", tags=["recipes"])


# Paginated response model
class PaginatedRecipes(BaseModel):
    """Paginated response for recipe lists."""
    items: List[RecipeListItem]
    total: int
    limit: int
    offset: int
    has_more: bool
    
    model_config = ConfigDict(from_attributes=True)


class RecipeUpdate(BaseModel):
    """Request to update a recipe (partial update - old style)."""
    title: Optional[str] = None
    servings: Optional[int] = None
    notes: Optional[str] = None
    tags: Optional[list[str]] = None
    is_public: Optional[bool] = None


class RecipeEdit(BaseModel):
    """Full recipe edit request."""
    title: str
    servings: Optional[int] = None
    prep_time: Optional[str] = None
    cook_time: Optional[str] = None
    total_time: Optional[str] = None
    ingredients: List["ManualIngredient"]
    steps: List[str]
    notes: Optional[str] = None
    tags: Optional[List[str]] = None
    is_public: Optional[bool] = None
    nutrition: Optional["ManualNutrition"] = None


class ManualIngredient(BaseModel):
    """Ingredient for manual recipe entry."""
    name: str
    quantity: Optional[str] = None
    unit: Optional[str] = None
    notes: Optional[str] = None


class ManualNutrition(BaseModel):
    """Nutrition data for manual recipe entry."""
    calories: Optional[int] = None
    protein: Optional[int] = None
    carbs: Optional[int] = None
    fat: Optional[int] = None


class ManualRecipeCreate(BaseModel):
    """Request to create a manual recipe."""
    title: str
    servings: Optional[int] = None
    prep_time: Optional[str] = None
    cook_time: Optional[str] = None
    total_time: Optional[str] = None
    ingredients: List[ManualIngredient]
    steps: List[str]
    notes: Optional[str] = None
    tags: Optional[List[str]] = None
    is_public: bool = True
    nutrition: Optional[ManualNutrition] = None


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


@router.post("/manual", response_model=RecipeResponse)
async def create_manual_recipe(
    recipe_data: str = Form(..., description="JSON string of ManualRecipeCreate"),
    image: Optional[UploadFile] = File(None, description="Optional recipe image"),
    db: AsyncSession = Depends(get_db),
    user: ClerkUser = Depends(get_current_user),
):
    """
    Create a recipe manually (not from video extraction).
    
    Accepts multipart form data with:
    - recipe_data: JSON string of the recipe details
    - image: Optional image file for the recipe thumbnail
    """
    try:
        # Parse the JSON recipe data
        data = json.loads(recipe_data)
        recipe_input = ManualRecipeCreate(**data)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON in recipe_data: {e}")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid recipe data: {e}")
    
    # Build the extracted JSONB structure to match extracted recipes
    ingredients_list = [
        {
            "name": ing.name,
            "quantity": ing.quantity,
            "unit": ing.unit,
            "notes": ing.notes,
        }
        for ing in recipe_input.ingredients
    ]
    
    extracted = {
        "title": recipe_input.title,
        "sourceUrl": "",  # Manual recipes don't have a source URL
        "servings": recipe_input.servings,
        "times": {
            "prep": recipe_input.prep_time,
            "cook": recipe_input.cook_time,
            "total": recipe_input.total_time,
        },
        "components": [
            {
                "name": "Main",
                "ingredients": ingredients_list,
                "steps": recipe_input.steps,
            }
        ],
        "ingredients": ingredients_list,  # Legacy field
        "steps": recipe_input.steps,  # Legacy field
        "equipment": [],
        "notes": recipe_input.notes,
        "tags": recipe_input.tags or [],
        "media": {"thumbnail": None},
        "totalEstimatedCost": None,
        "costLocation": "",
        "nutrition": {
            "perServing": {
                "calories": recipe_input.nutrition.calories if recipe_input.nutrition else None,
                "protein": recipe_input.nutrition.protein if recipe_input.nutrition else None,
                "carbs": recipe_input.nutrition.carbs if recipe_input.nutrition else None,
                "fat": recipe_input.nutrition.fat if recipe_input.nutrition else None,
                "fiber": None,
                "sugar": None,
                "sodium": None,
            },
            "total": {
                "calories": None,
                "protein": None,
                "carbs": None,
                "fat": None,
                "fiber": None,
                "sugar": None,
                "sodium": None,
            },
        },
    }
    
    # Create the recipe
    new_recipe = Recipe(
        source_url="manual://user-created",
        source_type="manual",
        raw_text=None,
        extracted=extracted,
        thumbnail_url=None,
        extraction_method="manual",
        extraction_quality=None,
        has_audio_transcript=False,
        user_id=user.id,
        is_public=recipe_input.is_public,
    )
    
    db.add(new_recipe)
    await db.commit()
    await db.refresh(new_recipe)
    
    # Upload image if provided
    if image and image.filename:
        try:
            image_data = await image.read()
            content_type = image.content_type or "image/jpeg"
            
            s3_url = await storage_service.upload_thumbnail_from_bytes(
                image_data,
                str(new_recipe.id),
                content_type
            )
            
            if s3_url:
                new_recipe.thumbnail_url = s3_url
                # Update the media field in extracted JSON
                new_recipe.extracted["media"]["thumbnail"] = s3_url
                await db.commit()
                await db.refresh(new_recipe)
        except Exception as e:
            print(f"⚠️ Failed to upload image: {e}")
            # Recipe is still created, just without an image
    
    return new_recipe


@router.get("/", response_model=PaginatedRecipes)
async def get_my_recipes(
    limit: int = Query(default=20, le=100, description="Max recipes to return"),
    offset: int = Query(default=0, ge=0, description="Number of recipes to skip"),
    source_type: Optional[str] = Query(default=None, description="Filter by source: tiktok, youtube, instagram"),
    db: AsyncSession = Depends(get_db),
    user: ClerkUser = Depends(get_current_user),
):
    """
    Get current user's recipes with pagination, ordered by most recent first.
    
    Requires authentication.
    """
    base_query = select(Recipe).where(Recipe.user_id == user.id)
    
    # Apply source_type filter if provided
    if source_type and source_type != 'all':
        base_query = base_query.where(Recipe.source_type == source_type)
    
    # Get total count
    count_result = await db.execute(select(func.count()).select_from(base_query.subquery()))
    total_count = count_result.scalar() or 0
    
    # Execute paginated query
    result = await db.execute(
        base_query.order_by(Recipe.created_at.desc()).offset(offset).limit(limit)
    )
    recipes = result.scalars().all()
    
    items = [recipe_to_list_item(r) for r in recipes]
    has_more = offset + len(items) < total_count
    
    return PaginatedRecipes(
        items=items,
        total=total_count,
        limit=limit,
        offset=offset,
        has_more=has_more,
    )


@router.get("/discover", response_model=PaginatedRecipes)
async def get_public_recipes(
    limit: int = Query(default=20, le=100, description="Max recipes to return"),
    offset: int = Query(default=0, ge=0, description="Number of recipes to skip"),
    source_type: Optional[str] = Query(default=None, description="Filter by source: tiktok, youtube, instagram"),
    db: AsyncSession = Depends(get_db),
    user: Optional[ClerkUser] = Depends(get_optional_user),
):
    """
    Get all public recipes (the shared library) with pagination.
    
    Works with or without authentication.
    """
    base_query = select(Recipe).where(Recipe.is_public == True)
    
    # Apply source_type filter if provided
    if source_type and source_type != 'all':
        base_query = base_query.where(Recipe.source_type == source_type)
    
    # Get total count
    count_result = await db.execute(select(func.count()).select_from(base_query.subquery()))
    total_count = count_result.scalar() or 0
    
    # Execute paginated query
    result = await db.execute(
        base_query.order_by(Recipe.created_at.desc()).offset(offset).limit(limit)
    )
    recipes = result.scalars().all()
    
    items = [recipe_to_list_item(r) for r in recipes]
    has_more = offset + len(items) < total_count
    
    return PaginatedRecipes(
        items=items,
        total=total_count,
        limit=limit,
        offset=offset,
        has_more=has_more,
    )


@router.get("/count")
async def get_recipe_count(
    source_type: Optional[str] = Query(default=None, description="Filter by source: tiktok, youtube, instagram"),
    db: AsyncSession = Depends(get_db),
    user: ClerkUser = Depends(get_current_user),
):
    """Get total number of user's recipes."""
    query = select(func.count(Recipe.id)).where(Recipe.user_id == user.id)
    
    if source_type and source_type != 'all':
        query = query.where(Recipe.source_type == source_type)
    
    result = await db.execute(query)
    count = result.scalar()
    return {"count": count or 0}


@router.get("/discover/count")
async def get_public_recipe_count(
    source_type: Optional[str] = Query(default=None, description="Filter by source: tiktok, youtube, instagram"),
    db: AsyncSession = Depends(get_db),
):
    """Get total number of public recipes."""
    query = select(func.count(Recipe.id)).where(Recipe.is_public == True)
    
    if source_type and source_type != 'all':
        query = query.where(Recipe.source_type == source_type)
    
    result = await db.execute(query)
    count = result.scalar()
    return {"count": count or 0}


def parse_time_to_minutes(time_str: str) -> Optional[int]:
    """Parse time string like '30 minutes', '1 hour', '1h 30m' to minutes."""
    if not time_str:
        return None
    
    time_str = time_str.lower().strip()
    total_minutes = 0
    
    # Handle "X hours" or "X hour"
    import re
    hours_match = re.search(r'(\d+)\s*(?:hours?|hrs?|h)', time_str)
    if hours_match:
        total_minutes += int(hours_match.group(1)) * 60
    
    # Handle "X minutes" or "X min"
    mins_match = re.search(r'(\d+)\s*(?:minutes?|mins?|m(?!onth))', time_str)
    if mins_match:
        total_minutes += int(mins_match.group(1))
    
    # Handle just a number (assume minutes)
    if total_minutes == 0:
        num_match = re.search(r'(\d+)', time_str)
        if num_match:
            total_minutes = int(num_match.group(1))
    
    return total_minutes if total_minutes > 0 else None


@router.get("/search", response_model=PaginatedRecipes)
async def search_recipes(
    q: str = Query(default="", description="Search query (searches title, ingredients, tags)"),
    limit: int = Query(default=20, le=100),
    offset: int = Query(default=0, ge=0),
    source_type: Optional[str] = Query(default=None, description="Filter by source: tiktok, youtube, instagram, manual"),
    time_filter: Optional[str] = Query(default=None, description="Filter by time: quick (<30min), medium (30-60min), long (60min+)"),
    tags: Optional[str] = Query(default=None, description="Comma-separated tags to filter by"),
    db: AsyncSession = Depends(get_db),
    user: ClerkUser = Depends(get_current_user),
):
    """
    Search and filter user's recipes with pagination.
    - Search across title, ingredients, and tags
    - Filter by source type, time, and tags
    - Returns paginated results with total count
    """
    # Start with base query
    base_query = select(Recipe).where(Recipe.user_id == user.id)
    
    # Full-text search across multiple fields
    if q and q.strip():
        search_term = f"%{q.lower()}%"
        base_query = base_query.where(
            or_(
                # Search in title
                func.lower(Recipe.extracted["title"].astext).like(search_term),
                # Search in tags array (cast to text)
                Recipe.extracted["tags"].astext.ilike(search_term),
                # Search in ingredients (JSONB contains - searches nested structure)
                func.lower(func.cast(Recipe.extracted["components"], String)).like(search_term),
                # Search in instructions
                func.lower(func.cast(Recipe.extracted["steps"], String)).like(search_term),
            )
        )
    
    # Filter by source type
    if source_type and source_type != 'all':
        base_query = base_query.where(Recipe.source_type == source_type)
    
    # Filter by tags (comma-separated)
    if tags:
        tag_list = [t.strip().lower() for t in tags.split(',') if t.strip()]
        for tag in tag_list:
            base_query = base_query.where(
                func.lower(Recipe.extracted["tags"].astext).like(f"%{tag}%")
            )
    
    # Get total count (before pagination, but after search/filters except time)
    count_result = await db.execute(select(func.count()).select_from(base_query.subquery()))
    total_count = count_result.scalar() or 0
    
    # Execute paginated query
    result = await db.execute(
        base_query.order_by(Recipe.created_at.desc()).offset(offset).limit(limit)
    )
    recipes = list(result.scalars().all())
    
    # Filter by time in Python (since time parsing is complex for SQL)
    if time_filter and time_filter != 'all':
        filtered_recipes = []
        for recipe in recipes:
            total_time = recipe.extracted.get("times", {}).get("total") or recipe.extracted.get("total_time")
            minutes = parse_time_to_minutes(total_time) if total_time else None
            
            if minutes is None:
                # Include recipes without time info only if not filtering
                continue
            elif time_filter == 'quick' and minutes < 30:
                filtered_recipes.append(recipe)
            elif time_filter == 'medium' and 30 <= minutes <= 60:
                filtered_recipes.append(recipe)
            elif time_filter == 'long' and minutes > 60:
                filtered_recipes.append(recipe)
        
        recipes = filtered_recipes
    
    items = [recipe_to_list_item(r) for r in recipes]
    has_more = offset + len(items) < total_count
    
    return PaginatedRecipes(
        items=items,
        total=total_count,
        limit=limit,
        offset=offset,
        has_more=has_more,
    )


@router.get("/discover/search", response_model=PaginatedRecipes)
async def search_public_recipes(
    q: str = Query(default="", description="Search query (searches title, ingredients, tags)"),
    limit: int = Query(default=20, le=100),
    offset: int = Query(default=0, ge=0),
    source_type: Optional[str] = Query(default=None, description="Filter by source: tiktok, youtube, instagram, manual"),
    time_filter: Optional[str] = Query(default=None, description="Filter by time: quick (<30min), medium (30-60min), long (60min+)"),
    tags: Optional[str] = Query(default=None, description="Comma-separated tags to filter by"),
    db: AsyncSession = Depends(get_db),
):
    """
    Search and filter public recipes with pagination.
    - Search across title, ingredients, and tags
    - Filter by source type, time, and tags
    - Returns paginated results with total count
    """
    # Start with base query
    base_query = select(Recipe).where(Recipe.is_public == True)
    
    # Full-text search across multiple fields
    if q and q.strip():
        search_term = f"%{q.lower()}%"
        base_query = base_query.where(
            or_(
                # Search in title
                func.lower(Recipe.extracted["title"].astext).like(search_term),
                # Search in tags array (cast to text)
                Recipe.extracted["tags"].astext.ilike(search_term),
                # Search in ingredients (JSONB contains - searches nested structure)
                func.lower(func.cast(Recipe.extracted["components"], String)).like(search_term),
                # Search in instructions
                func.lower(func.cast(Recipe.extracted["steps"], String)).like(search_term),
            )
        )
    
    # Filter by source type
    if source_type and source_type != 'all':
        base_query = base_query.where(Recipe.source_type == source_type)
    
    # Filter by tags (comma-separated)
    if tags:
        tag_list = [t.strip().lower() for t in tags.split(',') if t.strip()]
        for tag in tag_list:
            base_query = base_query.where(
                func.lower(Recipe.extracted["tags"].astext).like(f"%{tag}%")
            )
    
    # Get total count (before pagination, but after search/filters except time)
    count_result = await db.execute(select(func.count()).select_from(base_query.subquery()))
    total_count = count_result.scalar() or 0
    
    # Execute paginated query
    result = await db.execute(
        base_query.order_by(Recipe.created_at.desc()).offset(offset).limit(limit)
    )
    recipes = list(result.scalars().all())
    
    # Filter by time in Python (since time parsing is complex for SQL)
    if time_filter and time_filter != 'all':
        filtered_recipes = []
        for recipe in recipes:
            total_time = recipe.extracted.get("times", {}).get("total") or recipe.extracted.get("total_time")
            minutes = parse_time_to_minutes(total_time) if total_time else None
            
            if minutes is None:
                continue
            elif time_filter == 'quick' and minutes < 30:
                filtered_recipes.append(recipe)
            elif time_filter == 'medium' and 30 <= minutes <= 60:
                filtered_recipes.append(recipe)
            elif time_filter == 'long' and minutes > 60:
                filtered_recipes.append(recipe)
        
        recipes = filtered_recipes
    
    items = [recipe_to_list_item(r) for r in recipes]
    has_more = offset + len(items) < total_count
    
    return PaginatedRecipes(
        items=items,
        total=total_count,
        limit=limit,
        offset=offset,
        has_more=has_more,
    )


@router.get("/tags/popular")
async def get_popular_tags(
    scope: str = Query(default="user", description="Scope: 'user' for user's tags, 'public' for all public recipe tags"),
    limit: int = Query(default=10, le=20),
    db: AsyncSession = Depends(get_db),
    user: Optional[ClerkUser] = Depends(get_optional_user),
):
    """
    Get popular tags with counts.
    - scope='user': Tags from user's own recipes (requires auth)
    - scope='public': Tags from all public recipes
    """
    from collections import Counter
    
    # Build query based on scope
    if scope == "user":
        if not user:
            return []
        query = select(Recipe.extracted["tags"]).where(Recipe.user_id == user.id)
    else:
        query = select(Recipe.extracted["tags"]).where(Recipe.is_public == True)
    
    result = await db.execute(query)
    rows = result.all()
    
    # Count all tags
    tag_counter = Counter()
    for row in rows:
        tags = row[0]  # This is the JSONB tags array
        if tags and isinstance(tags, list):
            for tag in tags:
                if tag and isinstance(tag, str):
                    tag_counter[tag.lower()] += 1
    
    # Get top tags
    top_tags = tag_counter.most_common(limit)
    
    return [{"tag": tag, "count": count} for tag, count in top_tags]


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
    Check if recipe with this URL already exists.
    
    First checks if current user has it, then checks for any public recipe.
    This prevents duplicate extractions and saves API costs.
    
    For TikTok, we match by video ID to handle different short URLs for the same video.
    """
    from app.services.video import VideoService
    
    print(f"🔍 Original URL: {url}")
    
    # Normalize the URL (resolve TikTok short URLs, etc.)
    normalized_url = await VideoService.normalize_url(url)
    print(f"🔍 Normalized URL: {normalized_url}")
    print(f"🔍 User ID: {user.id}")
    
    # For TikTok, extract video ID for matching
    video_id = VideoService.extract_tiktok_video_id(normalized_url)
    print(f"🔍 TikTok Video ID: {video_id}")
    
    # Build query conditions - match by exact URL or by video ID pattern
    if video_id:
        # For TikTok, match any URL containing this video ID
        url_condition = Recipe.source_url.like(f"%/video/{video_id}%")
    else:
        # For other platforms, match exact URL or normalized URL
        url_condition = or_(Recipe.source_url == url, Recipe.source_url == normalized_url)
    
    # First, check if the current user already has this recipe
    user_result = await db.execute(
        select(Recipe).where(
            url_condition,
            Recipe.user_id == user.id
        )
    )
    user_recipe = user_result.scalar_one_or_none()
    print(f"🔍 User recipe found: {user_recipe is not None}")
    
    if user_recipe:
        return {
            "exists": True,
            "owned_by_user": True,
            "is_public": user_recipe.is_public,
            "recipe_id": str(user_recipe.id),
            "title": user_recipe.extracted.get("title", "Untitled") if user_recipe.extracted else "Untitled",
        }
    
    # Check if any PUBLIC recipe exists with this URL (from any user)
    public_result = await db.execute(
        select(Recipe).where(
            url_condition,
            Recipe.is_public == True
        ).limit(1)
    )
    public_recipe = public_result.scalar_one_or_none()
    print(f"🔍 Public recipe found: {public_recipe is not None}")
    
    if public_recipe:
        return {
            "exists": True,
            "owned_by_user": False,
            "is_public": True,
            "recipe_id": str(public_recipe.id),
            "title": public_recipe.extracted.get("title", "Untitled") if public_recipe.extracted else "Untitled",
        }
    
    return {"exists": False, "owned_by_user": False}


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


@router.patch("/{recipe_id}", response_model=RecipeResponse)
async def edit_recipe(
    recipe_id: UUID,
    edit: RecipeEdit,
    db: AsyncSession = Depends(get_db),
    user: ClerkUser = Depends(get_current_user),
):
    """
    Fully edit a recipe's content.
    
    For extracted recipes: saves original to original_extracted on first edit.
    For manual recipes: just updates directly.
    Only the recipe owner can edit.
    """
    result = await db.execute(
        select(Recipe).where(Recipe.id == recipe_id)
    )
    recipe = result.scalar_one_or_none()
    
    if not recipe:
        raise HTTPException(status_code=404, detail="Recipe not found")
    
    # Only owner can edit
    if recipe.user_id != user.id:
        raise HTTPException(status_code=403, detail="You can only edit your own recipes")
    
    # For extracted recipes, save original on first edit
    if recipe.source_type != "manual" and recipe.original_extracted is None:
        recipe.original_extracted = dict(recipe.extracted) if recipe.extracted else {}
    
    # Build the new extracted structure
    ingredients_list = [
        {
            "name": ing.name,
            "quantity": ing.quantity,
            "unit": ing.unit,
            "notes": ing.notes,
        }
        for ing in edit.ingredients
    ]
    
    # Preserve some fields from original extracted data
    old_extracted = recipe.extracted or {}
    
    new_extracted = {
        "title": edit.title,
        "sourceUrl": old_extracted.get("sourceUrl", ""),
        "servings": edit.servings,
        "times": {
            "prep": edit.prep_time,
            "cook": edit.cook_time,
            "total": edit.total_time,
        },
        "components": [
            {
                "name": "Main",
                "ingredients": ingredients_list,
                "steps": edit.steps,
            }
        ],
        "ingredients": ingredients_list,  # Legacy field
        "steps": edit.steps,  # Legacy field
        "equipment": old_extracted.get("equipment", []),
        "notes": edit.notes,
        "tags": edit.tags or [],
        "media": old_extracted.get("media", {"thumbnail": None}),
        "totalEstimatedCost": old_extracted.get("totalEstimatedCost"),
        "costLocation": old_extracted.get("costLocation", ""),
        "nutrition": {
            "perServing": {
                "calories": edit.nutrition.calories if edit.nutrition else old_extracted.get("nutrition", {}).get("perServing", {}).get("calories"),
                "protein": edit.nutrition.protein if edit.nutrition else old_extracted.get("nutrition", {}).get("perServing", {}).get("protein"),
                "carbs": edit.nutrition.carbs if edit.nutrition else old_extracted.get("nutrition", {}).get("perServing", {}).get("carbs"),
                "fat": edit.nutrition.fat if edit.nutrition else old_extracted.get("nutrition", {}).get("perServing", {}).get("fat"),
                "fiber": old_extracted.get("nutrition", {}).get("perServing", {}).get("fiber"),
                "sugar": old_extracted.get("nutrition", {}).get("perServing", {}).get("sugar"),
                "sodium": old_extracted.get("nutrition", {}).get("perServing", {}).get("sodium"),
            },
            "total": old_extracted.get("nutrition", {}).get("total", {}),
        },
    }
    
    recipe.extracted = new_extracted
    
    # Update is_public if provided
    if edit.is_public is not None:
        recipe.is_public = edit.is_public
    
    await db.commit()
    await db.refresh(recipe)
    
    return recipe


@router.post("/{recipe_id}/edit", response_model=RecipeResponse)
async def edit_recipe_with_image(
    recipe_id: UUID,
    recipe_data: str = Form(..., description="JSON string of RecipeEdit"),
    image: Optional[UploadFile] = File(None, description="Optional new recipe image"),
    db: AsyncSession = Depends(get_db),
    user: ClerkUser = Depends(get_current_user),
):
    """
    Edit a recipe with optional image upload.
    
    Accepts multipart form data with:
    - recipe_data: JSON string of the edit details
    - image: Optional new image file for the recipe thumbnail
    """
    try:
        data = json.loads(recipe_data)
        edit = RecipeEdit(**data)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON in recipe_data: {e}")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid recipe data: {e}")
    
    # Get the recipe
    result = await db.execute(
        select(Recipe).where(Recipe.id == recipe_id)
    )
    recipe = result.scalar_one_or_none()
    
    if not recipe:
        raise HTTPException(status_code=404, detail="Recipe not found")
    
    # Only owner can edit
    if recipe.user_id != user.id:
        raise HTTPException(status_code=403, detail="You can only edit your own recipes")
    
    # For extracted recipes, save original on first edit
    if recipe.source_type != "manual" and recipe.original_extracted is None:
        recipe.original_extracted = dict(recipe.extracted) if recipe.extracted else {}
    
    # Handle image upload
    thumbnail_url = recipe.thumbnail_url
    if image:
        try:
            image_bytes = await image.read()
            thumbnail_url = await storage_service.upload_thumbnail_from_bytes(
                image_bytes, str(recipe_id)
            )
        except Exception as e:
            print(f"Failed to upload image: {e}")
            # Continue without updating the image
    
    # Build the new extracted structure
    ingredients_list = [
        {
            "name": ing.name,
            "quantity": ing.quantity,
            "unit": ing.unit,
            "notes": ing.notes,
        }
        for ing in edit.ingredients
    ]
    
    old_extracted = recipe.extracted or {}
    
    new_extracted = {
        "title": edit.title,
        "sourceUrl": old_extracted.get("sourceUrl", ""),
        "servings": edit.servings,
        "times": {
            "prep": edit.prep_time,
            "cook": edit.cook_time,
            "total": edit.total_time,
        },
        "components": [
            {
                "name": "Main",
                "ingredients": ingredients_list,
                "steps": edit.steps,
            }
        ],
        "ingredients": ingredients_list,
        "steps": edit.steps,
        "equipment": old_extracted.get("equipment", []),
        "notes": edit.notes,
        "tags": edit.tags or [],
        "media": {"thumbnail": thumbnail_url},
        "totalEstimatedCost": old_extracted.get("totalEstimatedCost"),
        "costLocation": old_extracted.get("costLocation", ""),
        "nutrition": {
            "perServing": {
                "calories": edit.nutrition.calories if edit.nutrition else old_extracted.get("nutrition", {}).get("perServing", {}).get("calories"),
                "protein": edit.nutrition.protein if edit.nutrition else old_extracted.get("nutrition", {}).get("perServing", {}).get("protein"),
                "carbs": edit.nutrition.carbs if edit.nutrition else old_extracted.get("nutrition", {}).get("perServing", {}).get("carbs"),
                "fat": edit.nutrition.fat if edit.nutrition else old_extracted.get("nutrition", {}).get("perServing", {}).get("fat"),
                "fiber": old_extracted.get("nutrition", {}).get("perServing", {}).get("fiber"),
                "sugar": old_extracted.get("nutrition", {}).get("perServing", {}).get("sugar"),
                "sodium": old_extracted.get("nutrition", {}).get("perServing", {}).get("sodium"),
            },
            "total": old_extracted.get("nutrition", {}).get("total", {}),
        },
    }
    
    recipe.extracted = new_extracted
    recipe.thumbnail_url = thumbnail_url
    
    if edit.is_public is not None:
        recipe.is_public = edit.is_public
    
    await db.commit()
    await db.refresh(recipe)
    
    return recipe


@router.post("/{recipe_id}/restore", response_model=RecipeResponse)
async def restore_original_recipe(
    recipe_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: ClerkUser = Depends(get_current_user),
):
    """
    Restore an edited recipe to its original AI-extracted version.
    
    Only works for extracted recipes that have been edited.
    """
    result = await db.execute(
        select(Recipe).where(Recipe.id == recipe_id)
    )
    recipe = result.scalar_one_or_none()
    
    if not recipe:
        raise HTTPException(status_code=404, detail="Recipe not found")
    
    # Only owner can restore
    if recipe.user_id != user.id:
        raise HTTPException(status_code=403, detail="You can only restore your own recipes")
    
    # Check if there's an original to restore
    if recipe.original_extracted is None:
        raise HTTPException(
            status_code=400, 
            detail="No original version available. This recipe hasn't been edited or is a manual recipe."
        )
    
    # Restore the original
    recipe.extracted = dict(recipe.original_extracted)
    recipe.original_extracted = None  # Clear the backup
    
    await db.commit()
    await db.refresh(recipe)
    
    return recipe


@router.get("/{recipe_id}/has-original")
async def check_has_original(
    recipe_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: ClerkUser = Depends(get_current_user),
):
    """
    Check if a recipe has an original version that can be restored.
    """
    result = await db.execute(
        select(Recipe).where(Recipe.id == recipe_id)
    )
    recipe = result.scalar_one_or_none()
    
    if not recipe:
        raise HTTPException(status_code=404, detail="Recipe not found")
    
    # Only owner can check
    if recipe.user_id != user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    
    return {
        "has_original": recipe.original_extracted is not None,
        "source_type": recipe.source_type,
    }


# ============================================================
# Saved/Bookmarked Recipes
# ============================================================

@router.post("/{recipe_id}/save")
async def save_recipe(
    recipe_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: ClerkUser = Depends(get_current_user),
):
    """
    Save/bookmark a public recipe to the user's collection.
    """
    # Check if recipe exists and is public
    result = await db.execute(
        select(Recipe).where(Recipe.id == recipe_id)
    )
    recipe = result.scalar_one_or_none()
    
    if not recipe:
        raise HTTPException(status_code=404, detail="Recipe not found")
    
    # Can't save your own recipe
    if recipe.user_id == user.id:
        raise HTTPException(status_code=400, detail="You can't save your own recipe")
    
    # Recipe must be public to save
    if not recipe.is_public:
        raise HTTPException(status_code=403, detail="This recipe is not public")
    
    # Check if already saved
    existing = await db.execute(
        select(SavedRecipe).where(
            SavedRecipe.user_id == user.id,
            SavedRecipe.recipe_id == recipe_id
        )
    )
    if existing.scalar_one_or_none():
        return {"saved": True, "message": "Recipe already saved"}
    
    # Create the save
    saved = SavedRecipe(user_id=user.id, recipe_id=recipe_id)
    db.add(saved)
    await db.commit()
    
    return {"saved": True, "message": "Recipe saved to your collection"}


@router.delete("/{recipe_id}/save")
async def unsave_recipe(
    recipe_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: ClerkUser = Depends(get_current_user),
):
    """
    Remove a saved recipe from the user's collection.
    """
    result = await db.execute(
        select(SavedRecipe).where(
            SavedRecipe.user_id == user.id,
            SavedRecipe.recipe_id == recipe_id
        )
    )
    saved = result.scalar_one_or_none()
    
    if not saved:
        return {"saved": False, "message": "Recipe was not saved"}
    
    await db.delete(saved)
    await db.commit()
    
    return {"saved": False, "message": "Recipe removed from your collection"}


@router.get("/{recipe_id}/saved")
async def check_recipe_saved(
    recipe_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: ClerkUser = Depends(get_current_user),
):
    """
    Check if a recipe is saved by the current user.
    """
    result = await db.execute(
        select(SavedRecipe).where(
            SavedRecipe.user_id == user.id,
            SavedRecipe.recipe_id == recipe_id
        )
    )
    saved = result.scalar_one_or_none()
    
    return {"is_saved": saved is not None}


@router.get("/saved/list", response_model=PaginatedRecipes)
async def get_saved_recipes(
    limit: int = Query(default=20, le=100),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
    user: ClerkUser = Depends(get_current_user),
):
    """
    Get all recipes saved by the current user with pagination.
    """
    # Get total count
    count_result = await db.execute(
        select(func.count())
        .select_from(SavedRecipe)
        .where(SavedRecipe.user_id == user.id)
    )
    total_count = count_result.scalar() or 0
    
    # Join SavedRecipe with Recipe to get the actual recipe data
    result = await db.execute(
        select(Recipe)
        .join(SavedRecipe, SavedRecipe.recipe_id == Recipe.id)
        .where(SavedRecipe.user_id == user.id)
        .order_by(SavedRecipe.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    recipes = result.scalars().all()
    
    items = [recipe_to_list_item(recipe) for recipe in recipes]
    has_more = offset + len(items) < total_count
    
    return PaginatedRecipes(
        items=items,
        total=total_count,
        limit=limit,
        offset=offset,
        has_more=has_more,
    )


@router.get("/saved/count")
async def get_saved_recipes_count(
    db: AsyncSession = Depends(get_db),
    user: ClerkUser = Depends(get_current_user),
):
    """
    Get count of saved recipes for the current user.
    """
    result = await db.execute(
        select(func.count(SavedRecipe.id))
        .where(SavedRecipe.user_id == user.id)
    )
    count = result.scalar()
    
    return {"count": count}
