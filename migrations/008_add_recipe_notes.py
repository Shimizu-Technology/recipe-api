"""
Migration 008: Add recipe_notes table

Creates:
- recipe_notes: User's private notes on recipes (own or saved)
"""

import asyncio
from sqlalchemy import text
from app.db.database import engine


async def upgrade():
    """Create recipe_notes table."""
    async with engine.begin() as conn:
        # Check if recipe_notes table exists
        result = await conn.execute(text("""
            SELECT table_name 
            FROM information_schema.tables 
            WHERE table_name = 'recipe_notes'
        """))
        
        if result.scalar_one_or_none() is None:
            # Create recipe_notes table
            await conn.execute(text("""
                CREATE TABLE recipe_notes (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    user_id VARCHAR(64) NOT NULL,
                    recipe_id UUID NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
                    note_text TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                    UNIQUE(user_id, recipe_id)
                );
            """))
            
            # Create indexes for fast lookups
            await conn.execute(text("""
                CREATE INDEX idx_recipe_notes_user_id ON recipe_notes(user_id);
            """))
            await conn.execute(text("""
                CREATE INDEX idx_recipe_notes_recipe_id ON recipe_notes(recipe_id);
            """))
            
            print("✅ Created recipe_notes table with indexes")
        else:
            print("ℹ️ Table 'recipe_notes' already exists. Skipping.")


async def downgrade():
    """Remove recipe_notes table."""
    async with engine.begin() as conn:
        await conn.execute(text("DROP TABLE IF EXISTS recipe_notes;"))
        print("✅ Downgrade complete: removed recipe_notes table")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "down":
        asyncio.run(downgrade())
    else:
        asyncio.run(upgrade())

