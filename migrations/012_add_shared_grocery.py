"""
Migration: Add shared grocery list tables

Run with: python -m migrations.012_add_shared_grocery
"""

import asyncio
from sqlalchemy import text
from app.db.database import engine


async def upgrade():
    """Add shared grocery list tables and update grocery_items."""
    async with engine.begin() as conn:
        # Create grocery_lists table
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS grocery_lists (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                name VARCHAR(255) NOT NULL DEFAULT 'Grocery List',
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            );
        """))
        print("✅ Created grocery_lists table")
        
        # Create grocery_list_members table
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS grocery_list_members (
                list_id UUID NOT NULL REFERENCES grocery_lists(id) ON DELETE CASCADE,
                user_id VARCHAR(64) NOT NULL,
                display_name VARCHAR(255),
                joined_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                PRIMARY KEY (list_id, user_id)
            );
        """))
        print("✅ Created grocery_list_members table")
        
        # Create index on user_id for fast lookups
        await conn.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_grocery_list_members_user_id 
            ON grocery_list_members(user_id);
        """))
        
        # Create grocery_list_invites table
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS grocery_list_invites (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                list_id UUID NOT NULL REFERENCES grocery_lists(id) ON DELETE CASCADE,
                invite_code VARCHAR(20) UNIQUE NOT NULL,
                created_by VARCHAR(64) NOT NULL,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                accepted_by VARCHAR(64),
                accepted_at TIMESTAMP WITH TIME ZONE
            );
        """))
        print("✅ Created grocery_list_invites table")
        
        # Create index on invite_code for fast lookups
        await conn.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_grocery_list_invites_code 
            ON grocery_list_invites(invite_code);
        """))
        
        # Add list_id and added_by_name columns to grocery_items
        # Check if columns exist first
        result = await conn.execute(text("""
            SELECT column_name 
            FROM information_schema.columns 
            WHERE table_name = 'grocery_items' AND column_name = 'list_id';
        """))
        if not result.fetchone():
            await conn.execute(text("""
                ALTER TABLE grocery_items 
                ADD COLUMN list_id UUID REFERENCES grocery_lists(id) ON DELETE CASCADE;
            """))
            print("✅ Added list_id column to grocery_items")
        
        result = await conn.execute(text("""
            SELECT column_name 
            FROM information_schema.columns 
            WHERE table_name = 'grocery_items' AND column_name = 'added_by_name';
        """))
        if not result.fetchone():
            await conn.execute(text("""
                ALTER TABLE grocery_items 
                ADD COLUMN added_by_name VARCHAR(255);
            """))
            print("✅ Added added_by_name column to grocery_items")
        
        # Add archived column for when user joins a shared list
        result = await conn.execute(text("""
            SELECT column_name 
            FROM information_schema.columns 
            WHERE table_name = 'grocery_items' AND column_name = 'archived';
        """))
        if not result.fetchone():
            await conn.execute(text("""
                ALTER TABLE grocery_items 
                ADD COLUMN archived BOOLEAN DEFAULT FALSE;
            """))
            print("✅ Added archived column to grocery_items")
        
        # Create index on list_id for fast lookups
        await conn.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_grocery_items_list_id 
            ON grocery_items(list_id);
        """))
        
        # Migrate existing grocery items to their own lists
        # Step 1: Find all users with grocery items that don't have a list
        users_result = await conn.execute(text("""
            SELECT DISTINCT user_id 
            FROM grocery_items 
            WHERE list_id IS NULL;
        """))
        users = users_result.fetchall()
        
        for (user_id,) in users:
            # Create a list for this user
            list_result = await conn.execute(text("""
                INSERT INTO grocery_lists (name) 
                VALUES ('Grocery List')
                RETURNING id;
            """))
            list_id = list_result.fetchone()[0]
            
            # Add user as member
            await conn.execute(text("""
                INSERT INTO grocery_list_members (list_id, user_id)
                VALUES (:list_id, :user_id);
            """), {"list_id": list_id, "user_id": user_id})
            
            # Update their items to belong to this list
            await conn.execute(text("""
                UPDATE grocery_items 
                SET list_id = :list_id
                WHERE user_id = :user_id AND list_id IS NULL;
            """), {"list_id": list_id, "user_id": user_id})
            
            print(f"  ✅ Migrated grocery items for user {user_id[:8]}...")
        
        print(f"✅ Migrated {len(users)} users' grocery items to lists")


async def downgrade():
    """Remove shared grocery list tables and columns."""
    async with engine.begin() as conn:
        # Remove columns from grocery_items
        await conn.execute(text("""
            ALTER TABLE grocery_items 
            DROP COLUMN IF EXISTS list_id,
            DROP COLUMN IF EXISTS added_by_name,
            DROP COLUMN IF EXISTS archived;
        """))
        print("✅ Removed columns from grocery_items")
        
        # Drop tables in order (invites first due to FK)
        await conn.execute(text("DROP TABLE IF EXISTS grocery_list_invites;"))
        await conn.execute(text("DROP TABLE IF EXISTS grocery_list_members;"))
        await conn.execute(text("DROP TABLE IF EXISTS grocery_lists;"))
        print("✅ Dropped shared grocery tables")


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) > 1 and sys.argv[1] == "down":
        asyncio.run(downgrade())
    else:
        asyncio.run(upgrade())
