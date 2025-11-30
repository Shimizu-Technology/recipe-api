"""
Migration: Add saved_recipes table for bookmarking/liking public recipes.

Run with: python -m migrations.006_add_saved_recipes
"""

import asyncio
from sqlalchemy import text
from app.db.database import engine


async def upgrade():
    """Add saved_recipes table."""
    async with engine.begin() as conn:
        # Check if table already exists
        result = await conn.execute(text("""
            SELECT table_name 
            FROM information_schema.tables 
            WHERE table_name = 'saved_recipes'
        """))
        exists = result.fetchone()
        
        if exists:
            print("Table 'saved_recipes' already exists, skipping...")
            return
        
        # Create saved_recipes table
        print("Creating 'saved_recipes' table...")
        await conn.execute(text("""
            CREATE TABLE saved_recipes (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                user_id VARCHAR(64) NOT NULL,
                recipe_id UUID NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                UNIQUE(user_id, recipe_id)
            );
        """))
        
        # Create indexes for fast lookups
        await conn.execute(text("""
            CREATE INDEX idx_saved_recipes_user_id ON saved_recipes(user_id);
        """))
        
        await conn.execute(text("""
            CREATE INDEX idx_saved_recipes_recipe_id ON saved_recipes(recipe_id);
        """))
        
        print("✅ Created saved_recipes table with indexes")


async def downgrade():
    """Remove saved_recipes table."""
    async with engine.begin() as conn:
        await conn.execute(text("DROP TABLE IF EXISTS saved_recipes;"))
        print("✅ Dropped saved_recipes table")


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) > 1 and sys.argv[1] == "down":
        asyncio.run(downgrade())
    else:
        asyncio.run(upgrade())

