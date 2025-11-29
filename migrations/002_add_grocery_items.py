"""
Migration: Add grocery_items table

Run with: python -m migrations.002_add_grocery_items
"""

import asyncio
from sqlalchemy import text
from app.db.database import engine


async def upgrade():
    """Add grocery_items table."""
    async with engine.begin() as conn:
        # Create grocery_items table
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS grocery_items (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                user_id VARCHAR(64) NOT NULL,
                name VARCHAR(255) NOT NULL,
                quantity VARCHAR(50),
                unit VARCHAR(50),
                notes VARCHAR(255),
                checked BOOLEAN NOT NULL DEFAULT FALSE,
                recipe_id UUID REFERENCES recipes(id) ON DELETE SET NULL,
                recipe_title VARCHAR(255),
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            );
        """))
        
        # Create index on user_id for fast lookups
        await conn.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_grocery_items_user_id 
            ON grocery_items(user_id);
        """))
        
        # Create index on checked status
        await conn.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_grocery_items_checked 
            ON grocery_items(user_id, checked);
        """))
        
        print("✅ Created grocery_items table")


async def downgrade():
    """Remove grocery_items table."""
    async with engine.begin() as conn:
        await conn.execute(text("DROP TABLE IF EXISTS grocery_items;"))
        print("✅ Dropped grocery_items table")


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) > 1 and sys.argv[1] == "down":
        asyncio.run(downgrade())
    else:
        asyncio.run(upgrade())

