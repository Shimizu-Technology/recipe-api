"""Recipe API endpoints - CRUD operations with user authentication."""

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File, Form
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, or_, String
from pydantic import BaseModel, ConfigDict
from uuid import UUID
from typing import Optional, List, Generic, TypeVar
import json

from app.db import get_db
from app.models.recipe import Recipe, SavedRecipe, RecipeNote, RecipeVersion
from app.models.schemas import RecipeResponse, RecipeListItem


def generate_change_summary(old_extracted: dict, new_extracted: dict) -> str:
    """
    Compare old and new recipe data to generate a detailed human-readable change summary.
    
    Shows specific changes like:
    - Title: "Old" → "New"
    - Modified: ingredient name
    - Added: 2 ingredients
    - Removed: 1 step
    """
    if not old_extracted or not new_extracted:
        return "Recipe updated"
    
    changes = []
    
    # Compare title
    old_title = old_extracted.get("title", "")
    new_title = new_extracted.get("title", "")
    if old_title != new_title:
        # Truncate long titles
        old_short = old_title[:30] + "..." if len(old_title) > 30 else old_title
        new_short = new_title[:30] + "..." if len(new_title) > 30 else new_title
        changes.append(f'Title: "{old_short}" → "{new_short}"')
    
    # Compare servings
    old_servings = old_extracted.get("servings")
    new_servings = new_extracted.get("servings")
    if old_servings != new_servings:
        changes.append(f"Servings: {old_servings or 'none'} → {new_servings or 'none'}")
    
    # Compare times
    old_times = old_extracted.get("times", {})
    new_times = new_extracted.get("times", {})
    if old_times != new_times:
        time_changes = []
        for key, label in [("prep", "prep"), ("cook", "cook"), ("total", "total")]:
            if old_times.get(key) != new_times.get(key):
                time_changes.append(label)
        if time_changes:
            changes.append(f"Times: {', '.join(time_changes)}")
    
    # Compare ingredients in detail
    old_ingredients = old_extracted.get("ingredients", [])
    new_ingredients = new_extracted.get("ingredients", [])
    if old_ingredients != new_ingredients:
        ingredient_changes = _compare_ingredients(old_ingredients, new_ingredients)
        changes.extend(ingredient_changes)
    
    # Compare steps in detail
    old_steps = old_extracted.get("steps", [])
    new_steps = new_extracted.get("steps", [])
    if old_steps != new_steps:
        step_changes = _compare_steps(old_steps, new_steps)
        changes.extend(step_changes)
    
    # Compare notes
    old_notes = old_extracted.get("notes") or ""
    new_notes = new_extracted.get("notes") or ""
    if old_notes != new_notes:
        if not old_notes and new_notes:
            changes.append("Added notes")
        elif old_notes and not new_notes:
            changes.append("Removed notes")
        else:
            changes.append("Modified notes")
    
    # Compare tags
    old_tags = set(old_extracted.get("tags") or [])
    new_tags = set(new_extracted.get("tags") or [])
    if old_tags != new_tags:
        added_tags = new_tags - old_tags
        removed_tags = old_tags - new_tags
        if added_tags:
            changes.append(f"Added tags: {', '.join(list(added_tags)[:3])}")
        if removed_tags:
            changes.append(f"Removed tags: {', '.join(list(removed_tags)[:3])}")
    
    # Compare nutrition
    old_nutrition = old_extracted.get("nutrition", {}).get("perServing", {})
    new_nutrition = new_extracted.get("nutrition", {}).get("perServing", {})
    if old_nutrition != new_nutrition:
        changes.append("Updated nutrition info")
    
    if not changes:
        return "Minor updates"
    
    # Join with newlines for readability (up to 5 changes shown)
    if len(changes) > 5:
        return "\n".join(changes[:5]) + f"\n... and {len(changes) - 5} more changes"
    return "\n".join(changes)


def _compare_ingredients(old_ingredients: list, new_ingredients: list) -> list:
    """Compare ingredient lists and return detailed changes."""
    changes = []
    
    # Build lookup by name for comparison
    old_by_name = {ing.get("name", "").lower(): ing for ing in old_ingredients}
    new_by_name = {ing.get("name", "").lower(): ing for ing in new_ingredients}
    
    old_names = set(old_by_name.keys())
    new_names = set(new_by_name.keys())
    
    # Find added ingredients
    added = new_names - old_names
    if added:
        if len(added) == 1:
            name = list(added)[0]
            # Find the actual name with proper casing
            for ing in new_ingredients:
                if ing.get("name", "").lower() == name:
                    changes.append(f"Added ingredient: {ing.get('name')}")
                    break
        else:
            changes.append(f"Added {len(added)} ingredients")
    
    # Find removed ingredients
    removed = old_names - new_names
    if removed:
        if len(removed) == 1:
            name = list(removed)[0]
            for ing in old_ingredients:
                if ing.get("name", "").lower() == name:
                    changes.append(f"Removed ingredient: {ing.get('name')}")
                    break
        else:
            changes.append(f"Removed {len(removed)} ingredients")
    
    # Find modified ingredients (same name but different details)
    common = old_names & new_names
    modified_count = 0
    modified_example = None
    for name in common:
        old_ing = old_by_name[name]
        new_ing = new_by_name[name]
        if old_ing != new_ing:
            modified_count += 1
            if modified_example is None:
                # Find the actual ingredient name with proper casing
                for ing in new_ingredients:
                    if ing.get("name", "").lower() == name:
                        modified_example = ing.get("name")
                        break
    
    if modified_count > 0:
        if modified_count == 1 and modified_example:
            changes.append(f"Modified ingredient: {modified_example}")
        else:
            changes.append(f"Modified {modified_count} ingredients")
    
    return changes


def _compare_steps(old_steps: list, new_steps: list) -> list:
    """Compare step lists and return detailed changes."""
    changes = []
    
    old_count = len(old_steps)
    new_count = len(new_steps)
    
    if new_count > old_count:
        changes.append(f"Added {new_count - old_count} step(s)")
    elif new_count < old_count:
        changes.append(f"Removed {old_count - new_count} step(s)")
    
    # Check for modified steps (comparing by position)
    min_count = min(old_count, new_count)
    modified_steps = []
    for i in range(min_count):
        if old_steps[i] != new_steps[i]:
            modified_steps.append(i + 1)  # 1-indexed for display
    
    if modified_steps:
        if len(modified_steps) <= 3:
            changes.append(f"Modified step(s): {', '.join(map(str, modified_steps))}")
        else:
            changes.append(f"Modified {len(modified_steps)} steps")
    
    return changes


async def create_recipe_version(
    db: AsyncSession,
    recipe: Recipe,
    change_type: str,
    user_id: str,
    change_summary: str = None,
    new_extracted: dict = None
) -> RecipeVersion:
    """
    Create a new version snapshot of a recipe.
    
    Args:
        db: Database session
        recipe: The recipe to snapshot (current state before changes)
        change_type: Type of change ('initial', 'edit', 're-extract')
        user_id: ID of user making the change
        change_summary: Optional description of what changed (auto-generated if new_extracted provided)
        new_extracted: The new extracted data (for generating change summary)
    
    Returns:
        The created RecipeVersion
    """
    # Get the next version number
    result = await db.execute(
        select(func.max(RecipeVersion.version_number))
        .where(RecipeVersion.recipe_id == recipe.id)
    )
    max_version = result.scalar() or 0
    next_version = max_version + 1
    
    # Auto-generate change summary if new_extracted is provided
    if change_summary is None and new_extracted is not None:
        change_summary = generate_change_summary(
            recipe.extracted if recipe.extracted else {},
            new_extracted
        )
    elif change_summary is None:
        change_summary = f"Recipe {change_type}"
    
    # Create the version
    version = RecipeVersion(
        recipe_id=recipe.id,
        version_number=next_version,
        extracted=dict(recipe.extracted) if recipe.extracted else {},
        thumbnail_url=recipe.thumbnail_url,
        change_type=change_type,
        change_summary=change_summary,
        created_by=user_id,
    )
    db.add(version)
    
    return version
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
    source_type: Optional[str] = "manual"  # Can be "manual" or "photo" (for edited OCR)


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
    # source_type can be "manual" or "photo" (for edited OCR recipes)
    source_type = recipe_input.source_type or "manual"
    source_url = "photo-upload" if source_type == "photo" else "manual://user-created"
    
    new_recipe = Recipe(
        source_url=source_url,
        source_type=source_type,
        raw_text=None,
        extracted=extracted,
        thumbnail_url=None,
        extraction_method="ocr" if source_type == "photo" else "manual",
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


class OCRRecipeCreate(BaseModel):
    """Request to save an OCR-extracted recipe."""
    extracted: dict  # The full extracted JSON from OCR
    is_public: bool = True


@router.post("/from-ocr", response_model=RecipeResponse)
async def save_ocr_recipe(
    ocr_data: OCRRecipeCreate,
    db: AsyncSession = Depends(get_db),
    user: ClerkUser = Depends(get_current_user),
):
    """
    Save a recipe extracted via OCR (photo scanning).
    
    Accepts the full extracted JSON and saves it as a new recipe.
    """
    extracted = ocr_data.extracted
    
    # Ensure sourceUrl is set
    if not extracted.get("sourceUrl"):
        extracted["sourceUrl"] = "photo-upload"
    
    # Create the recipe
    new_recipe = Recipe(
        source_url="photo-upload",
        source_type="photo",
        raw_text=None,
        extracted=extracted,
        thumbnail_url=None,
        extraction_method="ocr",
        extraction_quality="good",  # OCR extractions are typically good quality
        has_audio_transcript=False,
        user_id=user.id,
        is_public=ocr_data.is_public,
    )
    
    db.add(new_recipe)
    await db.commit()
    await db.refresh(new_recipe)
    
    print(f"✅ OCR recipe saved: {extracted.get('title', 'Untitled')} (ID: {new_recipe.id})")
    
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
    
    Creates a version snapshot before applying changes.
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
    
    # Build the new extracted structure FIRST (for change comparison)
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
    
    # Create a version snapshot BEFORE applying changes (with change comparison)
    await create_recipe_version(
        db=db,
        recipe=recipe,
        change_type="edit",
        user_id=user.id,
        new_extracted=new_extracted  # For generating change summary
    )
    
    # For extracted recipes, save original on first edit
    if recipe.source_type != "manual" and recipe.original_extracted is None:
        recipe.original_extracted = dict(recipe.extracted) if recipe.extracted else {}
    
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
    
    Creates a version snapshot before applying changes.
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
    
    # Handle image upload first
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
    
    # Build the new extracted structure FIRST (for change comparison)
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
    
    # Create a version snapshot BEFORE applying changes (with change comparison)
    await create_recipe_version(
        db=db,
        recipe=recipe,
        change_type="edit",
        user_id=user.id,
        new_extracted=new_extracted  # For generating change summary
    )
    
    # For extracted recipes, save original on first edit
    if recipe.source_type != "manual" and recipe.original_extracted is None:
        recipe.original_extracted = dict(recipe.extracted) if recipe.extracted else {}
    
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


class ReExtractRequest(BaseModel):
    """Request to re-extract a recipe."""
    location: str = "Guam"


class RecipeNoteRequest(BaseModel):
    """Request to create/update a personal note on a recipe."""
    note_text: str


class RecipeNoteResponse(BaseModel):
    """Response with a recipe note."""
    id: UUID
    recipe_id: UUID
    note_text: str
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    
    model_config = ConfigDict(from_attributes=True)


@router.post("/{recipe_id}/re-extract", response_model=RecipeResponse)
async def re_extract_recipe(
    recipe_id: UUID,
    request: ReExtractRequest,
    db: AsyncSession = Depends(get_db),
    user: ClerkUser = Depends(get_current_user),
):
    """
    Re-extract a recipe from its source URL.
    
    Only allowed for:
    - Recipe owners (can re-extract their own recipes)
    - Admin users (can re-extract any recipe) - set role: "admin" in Clerk public_metadata
    
    The recipe must have a valid source_url (not manual recipes).
    Updates the recipe with new extraction data while preserving the original.
    """
    from app.services import recipe_extractor, storage_service
    
    # Fetch the recipe
    result = await db.execute(select(Recipe).where(Recipe.id == recipe_id))
    recipe = result.scalar_one_or_none()
    
    if not recipe:
        raise HTTPException(status_code=404, detail="Recipe not found")
    
    # Check permissions: owner or admin (admin role from Clerk public_metadata)
    is_owner = recipe.user_id == user.id
    
    if not is_owner and not user.is_admin:
        raise HTTPException(
            status_code=403, 
            detail="You don't have permission to re-extract this recipe"
        )
    
    # Check if recipe can be re-extracted (has a valid source URL)
    if not recipe.source_url or recipe.source_url.startswith("manual://"):
        raise HTTPException(
            status_code=400,
            detail="Cannot re-extract manual recipes. Please edit them directly."
        )
    
    # Save the old extracted data BEFORE extraction for comparison
    old_extracted = dict(recipe.extracted) if recipe.extracted else {}
    old_thumbnail = recipe.thumbnail_url
    
    # Store original if not already stored
    if not recipe.original_extracted:
        recipe.original_extracted = recipe.extracted.copy() if recipe.extracted else None
    
    # Run extraction
    try:
        extraction_result = await recipe_extractor.extract(
            url=recipe.source_url,
            location=request.location,
            notes=""  # Don't use old notes
        )
        
        if not extraction_result.success:
            raise HTTPException(
                status_code=500,
                detail=f"Re-extraction failed: {extraction_result.error}"
            )
        
        new_extracted = extraction_result.recipe
        
        # Create version snapshot with comparison AFTER we have the new data
        # We store the OLD state and compare to NEW state for the summary
        change_summary = generate_change_summary(old_extracted, new_extracted)
        if change_summary == "Minor updates":
            change_summary = "Re-extracted with AI (no significant changes detected)"
        else:
            change_summary = f"Re-extracted with AI:\n{change_summary}"
        
        # Get the next version number
        result = await db.execute(
            select(func.max(RecipeVersion.version_number))
            .where(RecipeVersion.recipe_id == recipe.id)
        )
        max_version = result.scalar() or 0
        
        version = RecipeVersion(
            recipe_id=recipe.id,
            version_number=max_version + 1,
            extracted=old_extracted,  # Store the OLD state
            thumbnail_url=old_thumbnail,
            change_type="re-extract",
            change_summary=change_summary,
            created_by=user.id,
        )
        db.add(version)
        
        # Update the recipe with new data
        recipe.raw_text = extraction_result.raw_text
        recipe.extracted = new_extracted
        recipe.extraction_method = extraction_result.extraction_method
        recipe.extraction_quality = extraction_result.extraction_quality
        recipe.has_audio_transcript = extraction_result.has_audio_transcript
        
        # Update thumbnail if we got a new one
        if extraction_result.thumbnail_url:
            s3_url = await storage_service.upload_thumbnail_from_url(
                extraction_result.thumbnail_url,
                str(recipe.id)
            )
            if s3_url:
                recipe.thumbnail_url = s3_url
                if recipe.extracted and "media" in recipe.extracted:
                    recipe.extracted["media"]["thumbnail"] = s3_url
        
        await db.commit()
        await db.refresh(recipe)
        
        return recipe
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Re-extraction failed: {str(e)}"
        )


# ============================================================
# Personal Recipe Notes
# ============================================================

@router.get("/{recipe_id}/notes", response_model=Optional[RecipeNoteResponse])
async def get_recipe_note(
    recipe_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: ClerkUser = Depends(get_current_user),
):
    """
    Get the current user's personal note for a recipe.
    
    Returns the note if it exists, otherwise null.
    Users can add notes to any recipe they can view (own or public).
    """
    # First verify the recipe exists and user can access it
    result = await db.execute(
        select(Recipe).where(Recipe.id == recipe_id)
    )
    recipe = result.scalar_one_or_none()
    
    if not recipe:
        raise HTTPException(status_code=404, detail="Recipe not found")
    
    # User must either own the recipe or it must be public
    if not recipe.is_public and recipe.user_id != user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    
    # Get the user's note for this recipe
    result = await db.execute(
        select(RecipeNote).where(
            RecipeNote.user_id == user.id,
            RecipeNote.recipe_id == recipe_id
        )
    )
    note = result.scalar_one_or_none()
    
    if not note:
        return None
    
    return RecipeNoteResponse(
        id=note.id,
        recipe_id=note.recipe_id,
        note_text=note.note_text,
        created_at=note.created_at.isoformat() if note.created_at else None,
        updated_at=note.updated_at.isoformat() if note.updated_at else None,
    )


@router.put("/{recipe_id}/notes", response_model=RecipeNoteResponse)
async def update_recipe_note(
    recipe_id: UUID,
    request: RecipeNoteRequest,
    db: AsyncSession = Depends(get_db),
    user: ClerkUser = Depends(get_current_user),
):
    """
    Create or update the current user's personal note for a recipe.
    
    Notes are private - only visible to the user who created them.
    Users can add notes to any recipe they can view (own or public).
    """
    # First verify the recipe exists and user can access it
    result = await db.execute(
        select(Recipe).where(Recipe.id == recipe_id)
    )
    recipe = result.scalar_one_or_none()
    
    if not recipe:
        raise HTTPException(status_code=404, detail="Recipe not found")
    
    # User must either own the recipe or it must be public
    if not recipe.is_public and recipe.user_id != user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    
    # Check if note already exists
    result = await db.execute(
        select(RecipeNote).where(
            RecipeNote.user_id == user.id,
            RecipeNote.recipe_id == recipe_id
        )
    )
    note = result.scalar_one_or_none()
    
    if note:
        # Update existing note
        note.note_text = request.note_text
    else:
        # Create new note
        note = RecipeNote(
            user_id=user.id,
            recipe_id=recipe_id,
            note_text=request.note_text
        )
        db.add(note)
    
    await db.commit()
    await db.refresh(note)
    
    return RecipeNoteResponse(
        id=note.id,
        recipe_id=note.recipe_id,
        note_text=note.note_text,
        created_at=note.created_at.isoformat() if note.created_at else None,
        updated_at=note.updated_at.isoformat() if note.updated_at else None,
    )


@router.delete("/{recipe_id}/notes")
async def delete_recipe_note(
    recipe_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: ClerkUser = Depends(get_current_user),
):
    """
    Delete the current user's personal note for a recipe.
    """
    result = await db.execute(
        select(RecipeNote).where(
            RecipeNote.user_id == user.id,
            RecipeNote.recipe_id == recipe_id
        )
    )
    note = result.scalar_one_or_none()
    
    if not note:
        return {"deleted": False, "message": "Note not found"}
    
    await db.delete(note)
    await db.commit()
    
    return {"deleted": True, "message": "Note deleted"}


# ============================================================
# Recipe Version History
# ============================================================

class RecipeVersionResponse(BaseModel):
    """Response with a recipe version."""
    id: UUID
    recipe_id: UUID
    version_number: int
    change_type: str
    change_summary: Optional[str] = None
    created_by: Optional[str] = None
    created_at: Optional[str] = None
    # Include title from extracted for display
    title: Optional[str] = None
    
    model_config = ConfigDict(from_attributes=True)


@router.get("/{recipe_id}/versions", response_model=List[RecipeVersionResponse])
async def get_recipe_versions(
    recipe_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: ClerkUser = Depends(get_current_user),
):
    """
    Get all versions of a recipe.
    
    Only accessible by the recipe owner.
    Returns versions in reverse chronological order (newest first).
    """
    # First verify the recipe exists and user owns it
    result = await db.execute(
        select(Recipe).where(Recipe.id == recipe_id)
    )
    recipe = result.scalar_one_or_none()
    
    if not recipe:
        raise HTTPException(status_code=404, detail="Recipe not found")
    
    # Only owner can view version history
    if recipe.user_id != user.id:
        raise HTTPException(status_code=403, detail="Only the recipe owner can view version history")
    
    # Get all versions
    result = await db.execute(
        select(RecipeVersion)
        .where(RecipeVersion.recipe_id == recipe_id)
        .order_by(RecipeVersion.version_number.desc())
    )
    versions = result.scalars().all()
    
    return [
        RecipeVersionResponse(
            id=v.id,
            recipe_id=v.recipe_id,
            version_number=v.version_number,
            change_type=v.change_type,
            change_summary=v.change_summary,
            created_by=v.created_by,
            created_at=v.created_at.isoformat() if v.created_at else None,
            title=v.extracted.get("title") if v.extracted else None,
        )
        for v in versions
    ]


@router.get("/{recipe_id}/versions/{version_id}")
async def get_recipe_version_detail(
    recipe_id: UUID,
    version_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: ClerkUser = Depends(get_current_user),
):
    """
    Get full details of a specific version.
    
    Returns the complete extracted data for that version.
    """
    # First verify the recipe exists and user owns it
    result = await db.execute(
        select(Recipe).where(Recipe.id == recipe_id)
    )
    recipe = result.scalar_one_or_none()
    
    if not recipe:
        raise HTTPException(status_code=404, detail="Recipe not found")
    
    if recipe.user_id != user.id:
        raise HTTPException(status_code=403, detail="Only the recipe owner can view version history")
    
    # Get the specific version
    result = await db.execute(
        select(RecipeVersion).where(
            RecipeVersion.id == version_id,
            RecipeVersion.recipe_id == recipe_id
        )
    )
    version = result.scalar_one_or_none()
    
    if not version:
        raise HTTPException(status_code=404, detail="Version not found")
    
    return {
        "id": str(version.id),
        "recipe_id": str(version.recipe_id),
        "version_number": version.version_number,
        "extracted": version.extracted,
        "thumbnail_url": version.thumbnail_url,
        "change_type": version.change_type,
        "change_summary": version.change_summary,
        "created_by": version.created_by,
        "created_at": version.created_at.isoformat() if version.created_at else None,
    }


@router.post("/{recipe_id}/versions/{version_id}/restore", response_model=RecipeResponse)
async def restore_recipe_version(
    recipe_id: UUID,
    version_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: ClerkUser = Depends(get_current_user),
):
    """
    Restore a recipe to a specific version.
    
    Creates a new version snapshot of the current state before restoring.
    """
    # First verify the recipe exists and user owns it
    result = await db.execute(
        select(Recipe).where(Recipe.id == recipe_id)
    )
    recipe = result.scalar_one_or_none()
    
    if not recipe:
        raise HTTPException(status_code=404, detail="Recipe not found")
    
    if recipe.user_id != user.id:
        raise HTTPException(status_code=403, detail="Only the recipe owner can restore versions")
    
    # Get the version to restore
    result = await db.execute(
        select(RecipeVersion).where(
            RecipeVersion.id == version_id,
            RecipeVersion.recipe_id == recipe_id
        )
    )
    version_to_restore = result.scalar_one_or_none()
    
    if not version_to_restore:
        raise HTTPException(status_code=404, detail="Version not found")
    
    # Create a version snapshot of current state BEFORE restoring
    await create_recipe_version(
        db=db,
        recipe=recipe,
        change_type="edit",
        user_id=user.id,
        change_summary=f"Before restoring to version {version_to_restore.version_number}"
    )
    
    # Restore the recipe to the selected version
    recipe.extracted = version_to_restore.extracted
    if version_to_restore.thumbnail_url:
        recipe.thumbnail_url = version_to_restore.thumbnail_url
    
    await db.commit()
    await db.refresh(recipe)
    
    return recipe


@router.get("/{recipe_id}/versions/count")
async def get_recipe_version_count(
    recipe_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: ClerkUser = Depends(get_current_user),
):
    """
    Get the count of versions for a recipe.
    """
    # First verify the recipe exists and user owns it
    result = await db.execute(
        select(Recipe).where(Recipe.id == recipe_id)
    )
    recipe = result.scalar_one_or_none()
    
    if not recipe:
        raise HTTPException(status_code=404, detail="Recipe not found")
    
    if recipe.user_id != user.id:
        raise HTTPException(status_code=403, detail="Only the recipe owner can view version history")
    
    # Get version count
    result = await db.execute(
        select(func.count(RecipeVersion.id))
        .where(RecipeVersion.recipe_id == recipe_id)
    )
    count = result.scalar() or 0
    
    return {"count": count}
