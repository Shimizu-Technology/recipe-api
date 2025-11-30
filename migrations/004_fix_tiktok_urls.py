"""
Migration 004: Fix TikTok URLs

The previous migration stripped TikTok URLs down to just /video/ID format,
which doesn't work for viewing. This migration uses the oEmbed API to
recover the full URLs with usernames.
"""

import asyncio
import re
import httpx
from sqlalchemy import select
from app.db.database import AsyncSessionLocal
from app.models.recipe import Recipe


async def get_full_tiktok_url(video_id: str) -> str | None:
    """Use TikTok's oEmbed API to get the full URL for a video ID."""
    # Try constructing a URL that oEmbed can resolve
    test_url = f"https://www.tiktok.com/@tiktok/video/{video_id}"
    
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            # The oEmbed API returns the actual URL
            response = await client.get(
                f"https://www.tiktok.com/oembed?url={test_url}"
            )
            if response.status_code == 200:
                data = response.json()
                # The author_url gives us the username
                author_url = data.get("author_url", "")
                if author_url:
                    # Extract username from author_url
                    username_match = re.search(r'@([^/]+)', author_url)
                    if username_match:
                        username = username_match.group(1)
                        return f"https://www.tiktok.com/@{username}/video/{video_id}"
    except Exception as e:
        print(f"  oEmbed failed: {e}")
    
    return None


async def upgrade():
    """Fix TikTok URLs that were incorrectly normalized."""
    print("Starting TikTok URL fix migration...")
    
    async with AsyncSessionLocal() as db:
        # Find recipes with broken TikTok URLs (no @ in the URL)
        result = await db.execute(
            select(Recipe).where(
                Recipe.source_type == "tiktok",
                Recipe.source_url.like("https://www.tiktok.com/video/%")
            )
        )
        recipes = result.scalars().all()
        
        print(f"Found {len(recipes)} TikTok recipes with broken URLs to fix")
        
        fixed = 0
        failed = 0
        
        for recipe in recipes:
            old_url = recipe.source_url
            
            # Extract video ID
            video_id_match = re.search(r'/video/(\d+)', old_url)
            if not video_id_match:
                print(f"  Could not extract video ID from: {old_url}")
                failed += 1
                continue
            
            video_id = video_id_match.group(1)
            
            # Try to get the full URL
            new_url = await get_full_tiktok_url(video_id)
            
            if new_url and new_url != old_url:
                recipe.source_url = new_url
                fixed += 1
                print(f"  Fixed: {old_url} -> {new_url}")
            else:
                print(f"  Could not fix: {old_url}")
                failed += 1
            
            # Rate limit to avoid hitting TikTok too hard
            await asyncio.sleep(0.5)
        
        await db.commit()
        
        print(f"\nMigration complete:")
        print(f"   - Fixed: {fixed}")
        print(f"   - Failed: {failed}")


if __name__ == "__main__":
    asyncio.run(upgrade())
