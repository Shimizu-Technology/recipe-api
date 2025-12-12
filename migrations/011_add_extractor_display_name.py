"""
Migration 011: Add extractor_display_name to recipes table

Adds a column to store the display name of the user who extracted the recipe.
This allows showing "by Alanna" or "by lmshimizu" on Discover cards.

Display name is computed at extraction time from:
1. first_name + last_name (if both present)
2. first_name only (if no last name)
3. email prefix before @ (if no name)
4. "A chef" (fallback)
"""

import asyncio
from sqlalchemy import text
from app.db.database import engine


async def upgrade():
    """Add extractor_display_name column to recipes table."""
    async with engine.begin() as conn:
        # Check if column already exists
        result = await conn.execute(text("""
            SELECT column_name 
            FROM information_schema.columns 
            WHERE table_name = 'recipes' AND column_name = 'extractor_display_name'
        """))
        
        if result.scalar_one_or_none() is None:
            # Add the column
            await conn.execute(text("""
                ALTER TABLE recipes 
                ADD COLUMN extractor_display_name VARCHAR(100)
            """))
            print("✅ Added extractor_display_name column to recipes table")
        else:
            print("⏭️ extractor_display_name column already exists")


async def downgrade():
    """Remove extractor_display_name column."""
    async with engine.begin() as conn:
        await conn.execute(text("""
            ALTER TABLE recipes DROP COLUMN IF EXISTS extractor_display_name
        """))
        print("✅ Removed extractor_display_name column")


if __name__ == "__main__":
    asyncio.run(upgrade())
