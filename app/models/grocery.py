"""SQLAlchemy model for grocery items."""

from sqlalchemy import Column, String, Boolean, DateTime, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func
import uuid

from app.db.database import Base


class GroceryItem(Base):
    """Grocery list item model."""
    
    __tablename__ = "grocery_items"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(String(64), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    quantity = Column(String(50), nullable=True)
    unit = Column(String(50), nullable=True)
    notes = Column(String(255), nullable=True)
    checked = Column(Boolean, nullable=False, default=False)
    recipe_id = Column(UUID(as_uuid=True), ForeignKey("recipes.id", ondelete="SET NULL"), nullable=True)
    recipe_title = Column(String(255), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

