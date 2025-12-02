"""User management endpoints - account deletion for Apple compliance."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import delete

from app.db import get_db
from app.models.recipe import Recipe, SavedRecipe
from app.models.grocery import GroceryItem
from app.auth import get_current_user, ClerkUser
from app.services.storage import storage_service

router = APIRouter(prefix="/api/users", tags=["users"])


@router.delete("/me")
async def delete_account(
    db: AsyncSession = Depends(get_db),
    user: ClerkUser = Depends(get_current_user),
):
    """
    Delete the current user's account and all associated data.
    
    This permanently deletes:
    - All recipes created by the user
    - All saved/bookmarked recipes
    - All grocery list items
    - Recipe thumbnails from S3
    
    Required for Apple App Store compliance (Guideline 5.1.1v).
    """
    user_id = user.id
    
    try:
        # 1. Get all user's recipes to delete their thumbnails from S3
        from sqlalchemy import select
        result = await db.execute(
            select(Recipe).where(Recipe.user_id == user_id)
        )
        user_recipes = result.scalars().all()
        
        # Delete thumbnails from S3
        for recipe in user_recipes:
            if recipe.thumbnail_url and 's3.amazonaws.com' in (recipe.thumbnail_url or ''):
                try:
                    # Extract key from URL and delete
                    # URL format: https://bucket.s3.region.amazonaws.com/key
                    key = recipe.thumbnail_url.split('.amazonaws.com/')[-1]
                    await storage_service.delete_image(key)
                except Exception as e:
                    # Log but don't fail - orphaned S3 objects are acceptable
                    print(f"Warning: Failed to delete S3 object: {e}")
        
        # 2. Delete all saved recipes (bookmarks)
        await db.execute(
            delete(SavedRecipe).where(SavedRecipe.user_id == user_id)
        )
        
        # 3. Delete all grocery items
        await db.execute(
            delete(GroceryItem).where(GroceryItem.user_id == user_id)
        )
        
        # 4. Delete all recipes
        await db.execute(
            delete(Recipe).where(Recipe.user_id == user_id)
        )
        
        # Commit all deletions
        await db.commit()
        
        return {
            "message": "Account deleted successfully",
            "deleted": {
                "recipes": len(user_recipes),
            }
        }
        
    except Exception as e:
        await db.rollback()
        raise HTTPException(
            status_code=500,
            detail=f"Failed to delete account: {str(e)}"
        )

