"""
Migration: Add user_id and is_public columns to recipes table.

This migration:
1. Adds user_id column (nullable, for Clerk user ID)
2. Adds is_public column (boolean, default false)
3. Sets all existing recipes to is_public=true (they become the public library)

Run with: python -m migrations.001_add_user_and_public_fields
"""

import asyncio
import os
import sys

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text
from app.db.database import engine


async def run_migration():
    """Run the migration."""
    print("🔄 Starting migration: Add user_id and is_public fields...")
    
    async with engine.begin() as conn:
        # Check if columns already exist
        result = await conn.execute(text("""
            SELECT column_name 
            FROM information_schema.columns 
            WHERE table_name = 'recipes' 
            AND column_name IN ('user_id', 'is_public')
        """))
        existing_columns = [row[0] for row in result.fetchall()]
        
        # Add user_id column if it doesn't exist
        if 'user_id' not in existing_columns:
            print("  Adding user_id column...")
            await conn.execute(text("""
                ALTER TABLE recipes 
                ADD COLUMN user_id VARCHAR(64) DEFAULT NULL
            """))
            print("  ✅ user_id column added")
            
            # Add index for faster user lookups
            await conn.execute(text("""
                CREATE INDEX IF NOT EXISTS idx_recipes_user_id 
                ON recipes(user_id)
            """))
            print("  ✅ Index on user_id created")
        else:
            print("  ⏭️  user_id column already exists, skipping")
        
        # Add is_public column if it doesn't exist
        if 'is_public' not in existing_columns:
            print("  Adding is_public column...")
            await conn.execute(text("""
                ALTER TABLE recipes 
                ADD COLUMN is_public BOOLEAN NOT NULL DEFAULT false
            """))
            print("  ✅ is_public column added")
            
            # Set all existing recipes to public (they become the shared library)
            result = await conn.execute(text("""
                UPDATE recipes 
                SET is_public = true 
                WHERE user_id IS NULL
            """))
            print(f"  ✅ Set {result.rowcount} existing recipes to public")
            
            # Add index for faster public recipe queries
            await conn.execute(text("""
                CREATE INDEX IF NOT EXISTS idx_recipes_is_public 
                ON recipes(is_public) WHERE is_public = true
            """))
            print("  ✅ Index on is_public created")
        else:
            print("  ⏭️  is_public column already exists, skipping")
    
    print("✅ Migration complete!")


if __name__ == "__main__":
    asyncio.run(run_migration())

