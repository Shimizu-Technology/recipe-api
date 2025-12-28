"""
Migration 013: Add low confidence extraction warning columns to extraction_jobs table.

These columns store extraction confidence info to display warnings to users.
"""

import asyncio
from sqlalchemy import text
from app.db.database import engine


async def run_migration():
    """Add low_confidence and confidence_warning columns to extraction_jobs table."""
    
    async with engine.begin() as conn:
        # Check if low_confidence column already exists
        result = await conn.execute(text("""
            SELECT column_name 
            FROM information_schema.columns 
            WHERE table_name = 'extraction_jobs' AND column_name = 'low_confidence'
        """))
        
        if not result.fetchone():
            # Add the low_confidence column
            await conn.execute(text("""
                ALTER TABLE extraction_jobs
                ADD COLUMN low_confidence BOOLEAN DEFAULT FALSE
            """))
            print("✓ Added low_confidence column to extraction_jobs")
        else:
            print("✓ low_confidence column already exists")
        
        # Check if confidence_warning column already exists
        result = await conn.execute(text("""
            SELECT column_name 
            FROM information_schema.columns 
            WHERE table_name = 'extraction_jobs' AND column_name = 'confidence_warning'
        """))
        
        if not result.fetchone():
            # Add the confidence_warning column
            await conn.execute(text("""
                ALTER TABLE extraction_jobs
                ADD COLUMN confidence_warning TEXT
            """))
            print("✓ Added confidence_warning column to extraction_jobs")
        else:
            print("✓ confidence_warning column already exists")


if __name__ == "__main__":
    asyncio.run(run_migration())
