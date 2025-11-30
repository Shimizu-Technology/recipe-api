"""
Migration 004b: Fix TikTok URLs (Batch Version)

Processes in batches and commits incrementally to avoid timeouts.
"""

import asyncio
import re
import httpx
from sqlalchemy import select, update
from app.db.database import AsyncSessionLocal
from app.models.recipe import Recipe

BATCH_SIZE = 20


async def get_full_tiktok_url(video_id: str) -> str | None:
    """Use TikTok's oEmbed API to get the full URL for a video ID."""
    test_url = f"https://www.tiktok.com/@tiktok/video/{video_id}"
    
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(
                f"https://www.tiktok.com/oembed?url={test_url}"
            )
            if response.status_code == 200:
                data = response.json()
                author_url = data.get("author_url", "")
                if author_url:
                    username_match = re.search(r'@([^/]+)', author_url)
                    if username_match:
                        username = username_match.group(1)
                        return f"https://www.tiktok.com/@{username}/video/{video_id}"
    except Exception as e:
        pass
    
    return None


async def process_batch(recipes):
    """Process a batch of recipes and return updates."""
    updates = []
    for recipe in recipes:
        video_id_match = re.search(r'/video/(\d+)', recipe.source_url)
        if video_id_match:
            video_id = video_id_match.group(1)
            new_url = await get_full_tiktok_url(video_id)
            if new_url:
                updates.append((recipe.id, new_url))
                print(f"  ✓ {video_id}")
            else:
                print(f"  ✗ {video_id} (API failed)")
            await asyncio.sleep(0.3)  # Rate limit
    return updates


async def upgrade():
    """Fix TikTok URLs in batches."""
    print("Starting TikTok URL fix (batch mode)...")
    
    total_fixed = 0
    total_failed = 0
    
    while True:
        async with AsyncSessionLocal() as db:
            # Get next batch of broken URLs
            result = await db.execute(
                select(Recipe).where(
                    Recipe.source_type == "tiktok",
                    Recipe.source_url.like("https://www.tiktok.com/video/%")
                ).limit(BATCH_SIZE)
            )
            recipes = result.scalars().all()
            
            if not recipes:
                break
            
            print(f"\nProcessing batch of {len(recipes)} recipes...")
            
            # Process batch
            updates = await process_batch(recipes)
            
            # Apply updates
            for recipe_id, new_url in updates:
                await db.execute(
                    update(Recipe).where(Recipe.id == recipe_id).values(source_url=new_url)
                )
            
            await db.commit()
            total_fixed += len(updates)
            total_failed += len(recipes) - len(updates)
            print(f"  Committed {len(updates)} fixes")
    
    print(f"\n✓ Migration complete: {total_fixed} fixed, {total_failed} failed")


if __name__ == "__main__":
    asyncio.run(upgrade())
