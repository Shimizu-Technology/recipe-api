"""Meal planning API endpoints."""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete, and_
from pydantic import BaseModel
from uuid import UUID
from typing import Optional, List
from datetime import date, datetime, timedelta

from app.db import get_db
from app.models.meal_plan import MealPlanEntry
from app.models.grocery import GroceryItem
from app.models.recipe import Recipe
from app.auth import get_current_user, ClerkUser

router = APIRouter(prefix="/api/meal-plans", tags=["meal-plans"])


# ============================================================
# Pydantic Schemas
# ============================================================

class MealPlanEntryCreate(BaseModel):
    """Request to add a meal to the plan."""
    date: date
    meal_type: str  # breakfast, lunch, dinner, snack
    recipe_id: UUID
    recipe_title: str
    recipe_thumbnail: Optional[str] = None
    notes: Optional[str] = None
    servings: Optional[str] = None


class MealPlanEntryUpdate(BaseModel):
    """Request to update a meal plan entry."""
    meal_type: Optional[str] = None
    date: Optional[date] = None
    notes: Optional[str] = None
    servings: Optional[str] = None


class MealPlanEntryResponse(BaseModel):
    """Meal plan entry response."""
    id: UUID
    date: date
    meal_type: str
    recipe_id: UUID
    recipe_title: str
    recipe_thumbnail: Optional[str] = None
    notes: Optional[str] = None
    servings: Optional[str] = None
    created_at: datetime
    
    class Config:
        from_attributes = True


class DayMeals(BaseModel):
    """All meals for a single day."""
    date: date
    breakfast: List[MealPlanEntryResponse] = []
    lunch: List[MealPlanEntryResponse] = []
    dinner: List[MealPlanEntryResponse] = []
    snack: List[MealPlanEntryResponse] = []


class WeekPlanResponse(BaseModel):
    """A full week's meal plan."""
    week_start: date
    week_end: date
    days: List[DayMeals]


class AddToGroceryRequest(BaseModel):
    """Request to add meal plan ingredients to grocery list."""
    start_date: date
    end_date: date


# ============================================================
# Helper Functions
# ============================================================

def get_week_bounds(target_date: date) -> tuple[date, date]:
    """Get the Monday and Sunday of the week containing target_date."""
    # Monday = 0, Sunday = 6
    days_since_monday = target_date.weekday()
    week_start = target_date - timedelta(days=days_since_monday)
    week_end = week_start + timedelta(days=6)
    return week_start, week_end


def organize_by_day(entries: List[MealPlanEntry], week_start: date, week_end: date) -> List[DayMeals]:
    """Organize entries into days with meal slots."""
    # Create a dict for quick lookup
    entries_by_date = {}
    for entry in entries:
        if entry.date not in entries_by_date:
            entries_by_date[entry.date] = {"breakfast": [], "lunch": [], "dinner": [], "snack": []}
        meal_type = entry.meal_type.lower()
        if meal_type in entries_by_date[entry.date]:
            entries_by_date[entry.date][meal_type].append(entry)
    
    # Build the response for each day of the week
    days = []
    current_date = week_start
    while current_date <= week_end:
        day_meals = entries_by_date.get(current_date, {"breakfast": [], "lunch": [], "dinner": [], "snack": []})
        days.append(DayMeals(
            date=current_date,
            breakfast=day_meals["breakfast"],
            lunch=day_meals["lunch"],
            dinner=day_meals["dinner"],
            snack=day_meals["snack"],
        ))
        current_date += timedelta(days=1)
    
    return days


# ============================================================
# Endpoints
# ============================================================

@router.get("/week", response_model=WeekPlanResponse)
async def get_week_plan(
    week_of: Optional[date] = Query(default=None, description="Any date in the target week (defaults to current week)"),
    db: AsyncSession = Depends(get_db),
    user: ClerkUser = Depends(get_current_user)
):
    """
    Get the meal plan for a specific week.
    
    Pass any date in the target week, or omit to get the current week.
    Returns Monday-Sunday with all meal slots.
    """
    target_date = week_of or date.today()
    week_start, week_end = get_week_bounds(target_date)
    
    # Fetch all entries for this week
    result = await db.execute(
        select(MealPlanEntry)
        .where(
            MealPlanEntry.user_id == user.id,
            MealPlanEntry.date >= week_start,
            MealPlanEntry.date <= week_end
        )
        .order_by(MealPlanEntry.date, MealPlanEntry.meal_type, MealPlanEntry.created_at)
    )
    entries = result.scalars().all()
    
    # Organize by day
    days = organize_by_day(entries, week_start, week_end)
    
    return WeekPlanResponse(
        week_start=week_start,
        week_end=week_end,
        days=days
    )


@router.get("/day", response_model=DayMeals)
async def get_day_plan(
    target_date: Optional[date] = Query(default=None, description="Target date (defaults to today)"),
    db: AsyncSession = Depends(get_db),
    user: ClerkUser = Depends(get_current_user)
):
    """Get the meal plan for a specific day."""
    target = target_date or date.today()
    
    result = await db.execute(
        select(MealPlanEntry)
        .where(
            MealPlanEntry.user_id == user.id,
            MealPlanEntry.date == target
        )
        .order_by(MealPlanEntry.meal_type, MealPlanEntry.created_at)
    )
    entries = result.scalars().all()
    
    # Organize into meal slots
    meals = {"breakfast": [], "lunch": [], "dinner": [], "snack": []}
    for entry in entries:
        meal_type = entry.meal_type.lower()
        if meal_type in meals:
            meals[meal_type].append(entry)
    
    return DayMeals(
        date=target,
        breakfast=meals["breakfast"],
        lunch=meals["lunch"],
        dinner=meals["dinner"],
        snack=meals["snack"],
    )


@router.post("/", response_model=MealPlanEntryResponse)
async def add_meal(
    entry: MealPlanEntryCreate,
    db: AsyncSession = Depends(get_db),
    user: ClerkUser = Depends(get_current_user)
):
    """Add a recipe to a meal slot."""
    # Validate meal_type
    valid_meal_types = ["breakfast", "lunch", "dinner", "snack"]
    if entry.meal_type.lower() not in valid_meal_types:
        raise HTTPException(
            status_code=400, 
            detail=f"Invalid meal_type. Must be one of: {', '.join(valid_meal_types)}"
        )
    
    new_entry = MealPlanEntry(
        user_id=user.id,
        date=entry.date,
        meal_type=entry.meal_type.lower(),
        recipe_id=entry.recipe_id,
        recipe_title=entry.recipe_title,
        recipe_thumbnail=entry.recipe_thumbnail,
        notes=entry.notes,
        servings=entry.servings,
    )
    
    db.add(new_entry)
    await db.commit()
    await db.refresh(new_entry)
    
    return new_entry


@router.put("/{entry_id}", response_model=MealPlanEntryResponse)
async def update_meal(
    entry_id: UUID,
    update: MealPlanEntryUpdate,
    db: AsyncSession = Depends(get_db),
    user: ClerkUser = Depends(get_current_user)
):
    """Update a meal plan entry (e.g., change date, notes, servings)."""
    result = await db.execute(
        select(MealPlanEntry).where(
            MealPlanEntry.id == entry_id,
            MealPlanEntry.user_id == user.id
        )
    )
    entry = result.scalar_one_or_none()
    
    if not entry:
        raise HTTPException(status_code=404, detail="Meal plan entry not found")
    
    if update.meal_type is not None:
        valid_meal_types = ["breakfast", "lunch", "dinner", "snack"]
        if update.meal_type.lower() not in valid_meal_types:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid meal_type. Must be one of: {', '.join(valid_meal_types)}"
            )
        entry.meal_type = update.meal_type.lower()
    if update.date is not None:
        entry.date = update.date
    if update.notes is not None:
        entry.notes = update.notes
    if update.servings is not None:
        entry.servings = update.servings
    
    await db.commit()
    await db.refresh(entry)
    
    return entry


@router.delete("/{entry_id}")
async def delete_meal(
    entry_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: ClerkUser = Depends(get_current_user)
):
    """Remove a recipe from the meal plan."""
    result = await db.execute(
        select(MealPlanEntry).where(
            MealPlanEntry.id == entry_id,
            MealPlanEntry.user_id == user.id
        )
    )
    entry = result.scalar_one_or_none()
    
    if not entry:
        raise HTTPException(status_code=404, detail="Meal plan entry not found")
    
    await db.delete(entry)
    await db.commit()
    
    return {"message": "Meal removed from plan", "id": str(entry_id)}


@router.delete("/day/{target_date}")
async def clear_day(
    target_date: date,
    meal_type: Optional[str] = Query(default=None, description="Clear only this meal type"),
    db: AsyncSession = Depends(get_db),
    user: ClerkUser = Depends(get_current_user)
):
    """Clear all meals for a specific day (or just one meal type)."""
    query = delete(MealPlanEntry).where(
        MealPlanEntry.user_id == user.id,
        MealPlanEntry.date == target_date
    )
    
    if meal_type:
        query = query.where(MealPlanEntry.meal_type == meal_type.lower())
    
    result = await db.execute(query.returning(MealPlanEntry.id))
    deleted_ids = result.scalars().all()
    await db.commit()
    
    return {
        "message": f"Cleared {len(deleted_ids)} meals from {target_date}",
        "count": len(deleted_ids)
    }


@router.post("/to-grocery")
async def add_plan_to_grocery(
    request: AddToGroceryRequest,
    db: AsyncSession = Depends(get_db),
    user: ClerkUser = Depends(get_current_user)
):
    """
    Add all ingredients from meal plan to grocery list.
    
    Fetches the recipes in the date range and adds their ingredients.
    """
    # Get all meal plan entries in the date range
    result = await db.execute(
        select(MealPlanEntry)
        .where(
            MealPlanEntry.user_id == user.id,
            MealPlanEntry.date >= request.start_date,
            MealPlanEntry.date <= request.end_date
        )
    )
    entries = result.scalars().all()
    
    if not entries:
        return {"message": "No meals planned in this date range", "items_added": 0}
    
    # Get unique recipe IDs
    recipe_ids = list(set(entry.recipe_id for entry in entries))
    
    # Fetch the full recipes to get ingredients
    recipes_result = await db.execute(
        select(Recipe).where(Recipe.id.in_(recipe_ids))
    )
    recipes = {r.id: r for r in recipes_result.scalars().all()}
    
    # Add ingredients to grocery list
    items_added = 0
    for entry in entries:
        recipe = recipes.get(entry.recipe_id)
        if not recipe or not recipe.extracted:
            continue
        
        extracted = recipe.extracted
        components = extracted.get("components", [])
        
        for component in components:
            ingredients = component.get("ingredients", [])
            for ing in ingredients:
                # Create grocery item
                grocery_item = GroceryItem(
                    user_id=user.id,
                    name=ing.get("name", "Unknown"),
                    quantity=ing.get("quantity"),
                    unit=ing.get("unit"),
                    notes=ing.get("notes"),
                    recipe_id=entry.recipe_id,
                    recipe_title=entry.recipe_title,
                    checked=False,
                )
                db.add(grocery_item)
                items_added += 1
    
    await db.commit()
    
    return {
        "message": f"Added {items_added} ingredients to grocery list",
        "items_added": items_added,
        "recipes_processed": len(recipe_ids)
    }


@router.post("/copy-week")
async def copy_week(
    source_week: date = Query(..., description="Any date in the source week"),
    target_week: date = Query(..., description="Any date in the target week"),
    db: AsyncSession = Depends(get_db),
    user: ClerkUser = Depends(get_current_user)
):
    """
    Copy a week's meal plan to another week.
    
    Useful for repeating a good week or planning ahead.
    """
    source_start, source_end = get_week_bounds(source_week)
    target_start, target_end = get_week_bounds(target_week)
    
    if source_start == target_start:
        raise HTTPException(status_code=400, detail="Source and target weeks are the same")
    
    # Get source week entries
    result = await db.execute(
        select(MealPlanEntry)
        .where(
            MealPlanEntry.user_id == user.id,
            MealPlanEntry.date >= source_start,
            MealPlanEntry.date <= source_end
        )
    )
    source_entries = result.scalars().all()
    
    if not source_entries:
        return {"message": "No meals to copy from source week", "entries_copied": 0}
    
    # Calculate the day offset
    day_offset = (target_start - source_start).days
    
    # Create new entries for target week
    entries_copied = 0
    for entry in source_entries:
        new_date = entry.date + timedelta(days=day_offset)
        new_entry = MealPlanEntry(
            user_id=user.id,
            date=new_date,
            meal_type=entry.meal_type,
            recipe_id=entry.recipe_id,
            recipe_title=entry.recipe_title,
            recipe_thumbnail=entry.recipe_thumbnail,
            notes=entry.notes,
            servings=entry.servings,
        )
        db.add(new_entry)
        entries_copied += 1
    
    await db.commit()
    
    return {
        "message": f"Copied {entries_copied} meals from {source_start} to {target_start}",
        "entries_copied": entries_copied,
        "target_week_start": target_start
    }

