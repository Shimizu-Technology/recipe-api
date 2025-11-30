"""
Migration 003: Normalize TikTok URLs

This migration resolves TikTok short URLs (tiktok.com/t/xxxxx) to their
canonical form (tiktok.com/video/123456789) so duplicate detection works
across different short URLs for the same video.
"""

import asyncio
import re
import httpx
from sqlalchemy import select
from app.db.database import AsyncSessionLocal
from app.models.recipe import Recipe


async def normalize_tiktok_url(url: str) -> str:
    """Resolve a TikTok short URL to its canonical form."""
    if not url or "tiktok.com" not in url.lower():
        return url
    
    # Already a full URL with video ID
    video_id_match = re.search(r'/video/(\d+)', url)
    if video_id_match:
        video_id = video_id_match.group(1)
        return f"https://www.tiktok.com/video/{video_id}"
    
    # Short URL - need to resolve
    if "/t/" in url or "vm.tiktok.com" in url:
        try:
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
                response = await client.head(url)
                resolved_url = str(response.url)
                print(f"  Resolved: {url} -> {resolved_url}")
                
                # Extract video ID from resolved URL
                video_id_match = re.search(r'/video/(\d+)', resolved_url)
                if video_id_match:
                    video_id = video_id_match.group(1)
                    return f"https://www.tiktok.com/video/{video_id}"
                
                return resolved_url
        except Exception as e:
            print(f"  Failed to resolve {url}: {e}")
            return url
    
    return url


async def upgrade():
    """Normalize all TikTok URLs in the database."""
    print("Starting TikTok URL normalization migration...")
    
    async with AsyncSessionLocal() as db:
        # Get all TikTok recipes
        result = await db.execute(
            select(Recipe).where(Recipe.source_type == "tiktok")
        )
        recipes = result.scalars().all()
        
        print(f"Found {len(recipes)} TikTok recipes to process")
        
        updated = 0
        skipped = 0
        failed = 0
        
        for recipe in recipes:
            old_url = recipe.source_url
            
            # Skip if already normalized
            if old_url and "/video/" in old_url and "/t/" not in old_url:
                print(f"  Already normalized: {old_url}")
                skipped += 1
                continue
            
            try:
                new_url = await normalize_tiktok_url(old_url)
                
                if new_url != old_url:
                    recipe.source_url = new_url
                    updated += 1
                    print(f"  Updated recipe {recipe.id}")
                else:
                    skipped += 1
            except Exception as e:
                print(f"  Failed to process recipe {recipe.id}: {e}")
                failed += 1
        
        await db.commit()
        
        print(f"\nMigration complete:")
        print(f"   - Updated: {updated}")
        print(f"   - Skipped: {skipped}")
        print(f"   - Failed: {failed}")


if __name__ == "__main__":
    asyncio.run(upgrade())
