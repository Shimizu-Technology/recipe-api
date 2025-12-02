"""
Migration 007: Add collections tables

Creates:
- collections: User's recipe collections/folders
- collection_recipes: Many-to-many relationship between collections and recipes
"""

import asyncio
from sqlalchemy import text
from app.db.database import engine


async def upgrade():
    """Create collections and collection_recipes tables."""
    async with engine.begin() as conn:
        # Check if collections table exists
        result = await conn.execute(text("""
            SELECT table_name 
            FROM information_schema.tables 
            WHERE table_name = 'collections'
        """))
        
        if result.scalar_one_or_none() is None:
            # Create collections table
            await conn.execute(text("""
                CREATE TABLE collections (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    user_id VARCHAR(64) NOT NULL,
                    name VARCHAR(100) NOT NULL,
                    emoji VARCHAR(10),
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                );
            """))
            
            # Create index on user_id for fast lookups
            await conn.execute(text("""
                CREATE INDEX idx_collections_user_id ON collections(user_id);
            """))
            
            print("✅ Created collections table with indexes")
        else:
            print("ℹ️ Table 'collections' already exists. Skipping.")
        
        # Check if collection_recipes table exists
        result = await conn.execute(text("""
            SELECT table_name 
            FROM information_schema.tables 
            WHERE table_name = 'collection_recipes'
        """))
        
        if result.scalar_one_or_none() is None:
            # Create collection_recipes junction table
            await conn.execute(text("""
                CREATE TABLE collection_recipes (
                    collection_id UUID NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
                    recipe_id UUID NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
                    added_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                    PRIMARY KEY (collection_id, recipe_id)
                );
            """))
            
            # Create indexes for fast lookups both ways
            await conn.execute(text("""
                CREATE INDEX idx_collection_recipes_collection_id ON collection_recipes(collection_id);
            """))
            await conn.execute(text("""
                CREATE INDEX idx_collection_recipes_recipe_id ON collection_recipes(recipe_id);
            """))
            
            print("✅ Created collection_recipes table with indexes")
        else:
            print("ℹ️ Table 'collection_recipes' already exists. Skipping.")


async def downgrade():
    """Remove collections tables."""
    async with engine.begin() as conn:
        await conn.execute(text("DROP TABLE IF EXISTS collection_recipes;"))
        await conn.execute(text("DROP TABLE IF EXISTS collections;"))
        print("✅ Downgrade complete: removed collections tables")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "down":
        asyncio.run(downgrade())
    else:
        asyncio.run(upgrade())

