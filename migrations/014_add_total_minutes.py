"""
Migration 014: Add total_minutes column to recipes table and backfill existing data.

This column stores the parsed total cook time in minutes for efficient SQL filtering.
Enables proper server-side filtering for time-based recipe queries.
"""

import asyncio
import re
from sqlalchemy import text
from app.db.database import engine


def parse_time_to_minutes(time_str: str) -> int | None:
    """Parse time string like '30 minutes', '1 hour', '1h 30m' to minutes."""
    if not time_str:
        return None
    
    time_str = time_str.lower().strip()
    total_minutes = 0
    
    # Handle "X hours" or "X hour"
    hours_match = re.search(r'(\d+)\s*(?:hours?|hrs?|h)', time_str)
    if hours_match:
        total_minutes += int(hours_match.group(1)) * 60
    
    # Handle "X minutes" or "X min"
    mins_match = re.search(r'(\d+)\s*(?:minutes?|mins?|m(?!onth))', time_str)
    if mins_match:
        total_minutes += int(mins_match.group(1))
    
    # Handle just a number (assume minutes)
    if total_minutes == 0:
        num_match = re.search(r'(\d+)', time_str)
        if num_match:
            total_minutes = int(num_match.group(1))
    
    return total_minutes if total_minutes > 0 else None


async def run_migration():
    """Add total_minutes column to recipes table and backfill existing data."""
    
    async with engine.begin() as conn:
        # Check if total_minutes column already exists
        result = await conn.execute(text("""
            SELECT column_name 
            FROM information_schema.columns 
            WHERE table_name = 'recipes' AND column_name = 'total_minutes'
        """))
        
        if not result.fetchone():
            # Add the total_minutes column with an index
            await conn.execute(text("""
                ALTER TABLE recipes
                ADD COLUMN total_minutes INTEGER
            """))
            print("✓ Added total_minutes column to recipes")
            
            # Create index for faster filtering
            await conn.execute(text("""
                CREATE INDEX IF NOT EXISTS ix_recipes_total_minutes 
                ON recipes (total_minutes)
            """))
            print("✓ Created index on total_minutes")
        else:
            print("✓ total_minutes column already exists")
        
        # Backfill existing recipes
        print("\n📊 Backfilling total_minutes for existing recipes...")
        
        # Fetch all recipes that need backfilling
        result = await conn.execute(text("""
            SELECT id, extracted
            FROM recipes
            WHERE total_minutes IS NULL AND extracted IS NOT NULL
        """))
        rows = result.fetchall()
        
        updated_count = 0
        for row in rows:
            recipe_id = row[0]
            extracted = row[1]
            
            if not extracted:
                continue
            
            # Try to get total time from extracted data
            times = extracted.get("times") or {}
            total_time = times.get("total") or extracted.get("total_time")
            
            if total_time:
                minutes = parse_time_to_minutes(str(total_time))
                if minutes:
                    await conn.execute(
                        text("UPDATE recipes SET total_minutes = :minutes WHERE id = :id"),
                        {"minutes": minutes, "id": recipe_id}
                    )
                    updated_count += 1
        
        print(f"✓ Backfilled {updated_count} recipes with total_minutes")


if __name__ == "__main__":
    asyncio.run(run_migration())
