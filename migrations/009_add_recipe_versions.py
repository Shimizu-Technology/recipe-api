"""
Migration 009: Add recipe_versions table

Creates:
- recipe_versions: Tracks all versions of a recipe (edits, re-extractions, etc.)
"""

import asyncio
from sqlalchemy import text
from app.db.database import engine


async def upgrade():
    """Create recipe_versions table."""
    async with engine.begin() as conn:
        # Check if recipe_versions table exists
        result = await conn.execute(text("""
            SELECT table_name 
            FROM information_schema.tables 
            WHERE table_name = 'recipe_versions'
        """))
        
        if result.scalar_one_or_none() is None:
            # Create recipe_versions table
            await conn.execute(text("""
                CREATE TABLE recipe_versions (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    recipe_id UUID NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
                    version_number INTEGER NOT NULL,
                    extracted JSONB NOT NULL,
                    thumbnail_url TEXT,
                    change_type VARCHAR(32) NOT NULL DEFAULT 'edit',
                    change_summary TEXT,
                    created_by VARCHAR(64),
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                    UNIQUE(recipe_id, version_number)
                );
            """))
            
            # Create indexes for fast lookups
            await conn.execute(text("""
                CREATE INDEX idx_recipe_versions_recipe_id ON recipe_versions(recipe_id);
            """))
            await conn.execute(text("""
                CREATE INDEX idx_recipe_versions_created_at ON recipe_versions(created_at);
            """))
            
            print("✅ Created recipe_versions table with indexes")
        else:
            print("ℹ️ Table 'recipe_versions' already exists. Skipping.")


async def downgrade():
    """Remove recipe_versions table."""
    async with engine.begin() as conn:
        await conn.execute(text("DROP TABLE IF EXISTS recipe_versions;"))
        print("✅ Downgrade complete: removed recipe_versions table")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "down":
        asyncio.run(downgrade())
    else:
        asyncio.run(upgrade())

