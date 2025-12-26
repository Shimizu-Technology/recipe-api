"""SQLAlchemy models for grocery items and shared lists."""

from sqlalchemy import Column, String, Boolean, DateTime, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
import uuid

from app.db.database import Base


class GroceryList(Base):
    """Grocery list that can be shared between users."""
    
    __tablename__ = "grocery_lists"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(255), nullable=False, default="Grocery List")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    
    # Relationships
    members = relationship("GroceryListMember", back_populates="grocery_list", cascade="all, delete-orphan")
    items = relationship("GroceryItem", back_populates="grocery_list", cascade="all, delete-orphan")
    invites = relationship("GroceryListInvite", back_populates="grocery_list", cascade="all, delete-orphan")


class GroceryListMember(Base):
    """Member of a shared grocery list."""
    
    __tablename__ = "grocery_list_members"
    
    list_id = Column(UUID(as_uuid=True), ForeignKey("grocery_lists.id", ondelete="CASCADE"), primary_key=True)
    user_id = Column(String(64), nullable=False, primary_key=True, index=True)
    display_name = Column(String(255), nullable=True)
    joined_at = Column(DateTime(timezone=True), server_default=func.now())
    
    # Relationships
    grocery_list = relationship("GroceryList", back_populates="members")


class GroceryListInvite(Base):
    """Invite to join a shared grocery list."""
    
    __tablename__ = "grocery_list_invites"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    list_id = Column(UUID(as_uuid=True), ForeignKey("grocery_lists.id", ondelete="CASCADE"), nullable=False)
    invite_code = Column(String(20), unique=True, nullable=False, index=True)
    created_by = Column(String(64), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    accepted_by = Column(String(64), nullable=True)
    accepted_at = Column(DateTime(timezone=True), nullable=True)
    
    # Relationships
    grocery_list = relationship("GroceryList", back_populates="invites")


class GroceryItem(Base):
    """Grocery list item model."""
    
    __tablename__ = "grocery_items"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(String(64), nullable=False, index=True)
    list_id = Column(UUID(as_uuid=True), ForeignKey("grocery_lists.id", ondelete="CASCADE"), nullable=True, index=True)
    name = Column(String(255), nullable=False)
    quantity = Column(String(50), nullable=True)
    unit = Column(String(50), nullable=True)
    notes = Column(String(255), nullable=True)
    checked = Column(Boolean, nullable=False, default=False)
    recipe_id = Column(UUID(as_uuid=True), ForeignKey("recipes.id", ondelete="SET NULL"), nullable=True)
    recipe_title = Column(String(255), nullable=True)
    added_by_name = Column(String(255), nullable=True)  # Who added this item
    archived = Column(Boolean, nullable=False, default=False)  # Hidden when user joins shared list
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    
    # Relationships
    grocery_list = relationship("GroceryList", back_populates="items")

