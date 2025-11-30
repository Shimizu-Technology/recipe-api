"""
Migration: Add original_extracted column to recipes table.

This column stores the original AI-extracted data before user edits.
- For extracted recipes: populated on first edit, allows "restore original"
- For manual recipes: stays NULL (no original to restore)

Run with: python -m migrations.005_add_original_extracted
"""

import asyncio
from sqlalchemy import text
from app.db.database import engine


async def upgrade():
    """Add original_extracted column to recipes table."""
    async with engine.begin() as conn:
        # Check if column already exists
        result = await conn.execute(text("""
            SELECT column_name 
            FROM information_schema.columns 
            WHERE table_name = 'recipes' AND column_name = 'original_extracted'
        """))
        exists = result.fetchone()
        
        if exists:
            print("Column 'original_extracted' already exists, skipping...")
            return
        
        # Add the column
        print("Adding 'original_extracted' column to recipes table...")
        await conn.execute(text("""
            ALTER TABLE recipes 
            ADD COLUMN original_extracted JSONB DEFAULT NULL
        """))
        
        print("✅ Migration complete: added original_extracted column")


async def downgrade():
    """Remove original_extracted column."""
    async with engine.begin() as conn:
        await conn.execute(text("""
            ALTER TABLE recipes DROP COLUMN IF EXISTS original_extracted
        """))
        print("✅ Removed original_extracted column")


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) > 1 and sys.argv[1] == "down":
        asyncio.run(downgrade())
    else:
        asyncio.run(upgrade())

