"""Grocery list API endpoints."""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, delete, update
from pydantic import BaseModel
from uuid import UUID
from typing import Optional
from datetime import datetime

from app.db import get_db
from app.models.grocery import GroceryItem
from app.auth import get_current_user, ClerkUser

router = APIRouter(prefix="/api/grocery", tags=["grocery"])


# ============================================================
# Pydantic Schemas
# ============================================================

class GroceryItemCreate(BaseModel):
    """Request to add a grocery item."""
    name: str
    quantity: Optional[str] = None
    unit: Optional[str] = None
    notes: Optional[str] = None
    recipe_id: Optional[UUID] = None
    recipe_title: Optional[str] = None


class GroceryItemUpdate(BaseModel):
    """Request to update a grocery item."""
    name: Optional[str] = None
    quantity: Optional[str] = None
    unit: Optional[str] = None
    notes: Optional[str] = None
    checked: Optional[bool] = None


class GroceryItemResponse(BaseModel):
    """Grocery item response."""
    id: UUID
    name: str
    quantity: Optional[str] = None
    unit: Optional[str] = None
    notes: Optional[str] = None
    checked: bool
    recipe_id: Optional[UUID] = None
    recipe_title: Optional[str] = None
    created_at: datetime
    
    class Config:
        from_attributes = True


class AddFromRecipeRequest(BaseModel):
    """Request to add ingredients from a recipe."""
    recipe_id: UUID
    recipe_title: str
    ingredients: list[GroceryItemCreate]


# ============================================================
# Endpoints
# ============================================================

@router.get("/", response_model=list[GroceryItemResponse])
async def get_grocery_list(
    include_checked: bool = Query(default=True, description="Include checked items"),
    db: AsyncSession = Depends(get_db),
    user: ClerkUser = Depends(get_current_user)
):
    """
    Get the user's grocery list.
    
    Returns items ordered by: unchecked first, then by creation date.
    """
    query = select(GroceryItem).where(GroceryItem.user_id == user.id)
    
    if not include_checked:
        query = query.where(GroceryItem.checked == False)
    
    # Order: unchecked first, then by created_at desc
    query = query.order_by(GroceryItem.checked, GroceryItem.created_at.desc())
    
    result = await db.execute(query)
    items = result.scalars().all()
    
    return items


@router.get("/count")
async def get_grocery_count(
    db: AsyncSession = Depends(get_db),
    user: ClerkUser = Depends(get_current_user)
):
    """Get count of grocery items (total and unchecked)."""
    # Total count
    total_result = await db.execute(
        select(func.count(GroceryItem.id)).where(GroceryItem.user_id == user.id)
    )
    total = total_result.scalar()
    
    # Unchecked count
    unchecked_result = await db.execute(
        select(func.count(GroceryItem.id)).where(
            GroceryItem.user_id == user.id,
            GroceryItem.checked == False
        )
    )
    unchecked = unchecked_result.scalar()
    
    return {
        "total": total,
        "unchecked": unchecked,
        "checked": total - unchecked
    }


@router.post("/", response_model=GroceryItemResponse)
async def add_grocery_item(
    item: GroceryItemCreate,
    db: AsyncSession = Depends(get_db),
    user: ClerkUser = Depends(get_current_user)
):
    """Add a single item to the grocery list."""
    new_item = GroceryItem(
        user_id=user.id,
        name=item.name,
        quantity=item.quantity,
        unit=item.unit,
        notes=item.notes,
        recipe_id=item.recipe_id,
        recipe_title=item.recipe_title,
        checked=False,
    )
    
    db.add(new_item)
    await db.commit()
    await db.refresh(new_item)
    
    return new_item


@router.post("/from-recipe", response_model=list[GroceryItemResponse])
async def add_from_recipe(
    request: AddFromRecipeRequest,
    db: AsyncSession = Depends(get_db),
    user: ClerkUser = Depends(get_current_user)
):
    """
    Add all ingredients from a recipe to the grocery list.
    
    This is a batch operation that adds multiple items at once.
    """
    new_items = []
    
    for ingredient in request.ingredients:
        new_item = GroceryItem(
            user_id=user.id,
            name=ingredient.name,
            quantity=ingredient.quantity,
            unit=ingredient.unit,
            notes=ingredient.notes,
            recipe_id=request.recipe_id,
            recipe_title=request.recipe_title,
            checked=False,
        )
        db.add(new_item)
        new_items.append(new_item)
    
    await db.commit()
    
    # Refresh all items to get their IDs
    for item in new_items:
        await db.refresh(item)
    
    return new_items


@router.put("/{item_id}", response_model=GroceryItemResponse)
async def update_grocery_item(
    item_id: UUID,
    update: GroceryItemUpdate,
    db: AsyncSession = Depends(get_db),
    user: ClerkUser = Depends(get_current_user)
):
    """Update a grocery item (e.g., toggle checked status)."""
    result = await db.execute(
        select(GroceryItem).where(
            GroceryItem.id == item_id,
            GroceryItem.user_id == user.id
        )
    )
    item = result.scalar_one_or_none()
    
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    
    # Update fields
    if update.name is not None:
        item.name = update.name
    if update.quantity is not None:
        item.quantity = update.quantity
    if update.unit is not None:
        item.unit = update.unit
    if update.notes is not None:
        item.notes = update.notes
    if update.checked is not None:
        item.checked = update.checked
    
    await db.commit()
    await db.refresh(item)
    
    return item


@router.put("/{item_id}/toggle", response_model=GroceryItemResponse)
async def toggle_grocery_item(
    item_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: ClerkUser = Depends(get_current_user)
):
    """Toggle the checked status of a grocery item."""
    result = await db.execute(
        select(GroceryItem).where(
            GroceryItem.id == item_id,
            GroceryItem.user_id == user.id
        )
    )
    item = result.scalar_one_or_none()
    
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    
    item.checked = not item.checked
    await db.commit()
    await db.refresh(item)
    
    return item


@router.delete("/{item_id}")
async def delete_grocery_item(
    item_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: ClerkUser = Depends(get_current_user)
):
    """Delete a single grocery item."""
    result = await db.execute(
        select(GroceryItem).where(
            GroceryItem.id == item_id,
            GroceryItem.user_id == user.id
        )
    )
    item = result.scalar_one_or_none()
    
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    
    await db.delete(item)
    await db.commit()
    
    return {"message": "Item deleted", "id": str(item_id)}


@router.delete("/clear/checked")
async def clear_checked_items(
    db: AsyncSession = Depends(get_db),
    user: ClerkUser = Depends(get_current_user)
):
    """Delete all checked items from the grocery list."""
    result = await db.execute(
        delete(GroceryItem).where(
            GroceryItem.user_id == user.id,
            GroceryItem.checked == True
        ).returning(GroceryItem.id)
    )
    deleted_ids = result.scalars().all()
    await db.commit()
    
    return {
        "message": f"Cleared {len(deleted_ids)} checked items",
        "count": len(deleted_ids)
    }


@router.delete("/clear/all")
async def clear_all_items(
    db: AsyncSession = Depends(get_db),
    user: ClerkUser = Depends(get_current_user)
):
    """Delete all items from the grocery list."""
    result = await db.execute(
        delete(GroceryItem).where(
            GroceryItem.user_id == user.id
        ).returning(GroceryItem.id)
    )
    deleted_ids = result.scalars().all()
    await db.commit()
    
    return {
        "message": f"Cleared {len(deleted_ids)} items",
        "count": len(deleted_ids)
    }


@router.delete("/clear/recipe/{recipe_id}")
async def clear_recipe_items(
    recipe_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: ClerkUser = Depends(get_current_user)
):
    """Delete all items from a specific recipe in the grocery list."""
    result = await db.execute(
        delete(GroceryItem).where(
            GroceryItem.user_id == user.id,
            GroceryItem.recipe_id == recipe_id
        ).returning(GroceryItem.id)
    )
    deleted_ids = result.scalars().all()
    await db.commit()
    
    return {
        "message": f"Cleared {len(deleted_ids)} items from recipe",
        "count": len(deleted_ids),
        "recipe_id": str(recipe_id)
    }

