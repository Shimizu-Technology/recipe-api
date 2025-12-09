"""
Migration 010: Add meal_plan_entries table

Creates:
- meal_plan_entries: Stores individual meal assignments (recipe to day/meal slot)
"""

import asyncio
from sqlalchemy import text
from app.db.database import engine


async def upgrade():
    """Create meal_plan_entries table."""
    async with engine.begin() as conn:
        # Check if meal_plan_entries table exists
        result = await conn.execute(text("""
            SELECT table_name 
            FROM information_schema.tables 
            WHERE table_name = 'meal_plan_entries'
        """))
        
        if result.scalar_one_or_none() is None:
            # Create meal_plan_entries table
            await conn.execute(text("""
                CREATE TABLE meal_plan_entries (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    user_id VARCHAR(64) NOT NULL,
                    date DATE NOT NULL,
                    meal_type VARCHAR(20) NOT NULL,
                    recipe_id UUID NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
                    recipe_title VARCHAR(255) NOT NULL,
                    recipe_thumbnail VARCHAR(500),
                    notes VARCHAR(500),
                    servings VARCHAR(20),
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                );
            """))
            
            # Create indexes for fast lookups
            await conn.execute(text("""
                CREATE INDEX idx_meal_plan_entries_user_id ON meal_plan_entries(user_id);
            """))
            await conn.execute(text("""
                CREATE INDEX idx_meal_plan_entries_date ON meal_plan_entries(date);
            """))
            await conn.execute(text("""
                CREATE INDEX idx_meal_plan_entries_user_date ON meal_plan_entries(user_id, date);
            """))
            
            print("✅ Created meal_plan_entries table with indexes")
        else:
            print("ℹ️ Table 'meal_plan_entries' already exists. Skipping.")


async def downgrade():
    """Remove meal_plan_entries table."""
    async with engine.begin() as conn:
        await conn.execute(text("DROP TABLE IF EXISTS meal_plan_entries;"))
        print("✅ Downgrade complete: removed meal_plan_entries table")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "down":
        asyncio.run(downgrade())
    else:
        asyncio.run(upgrade())

