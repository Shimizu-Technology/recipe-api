"""SQLAlchemy models for meal planning."""

from sqlalchemy import Column, String, DateTime, ForeignKey, Date, Enum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
import uuid
import enum

from app.db.database import Base


class MealType(str, enum.Enum):
    """Types of meals."""
    BREAKFAST = "breakfast"
    LUNCH = "lunch"
    DINNER = "dinner"
    SNACK = "snack"


class MealPlanEntry(Base):
    """A single meal in a meal plan (e.g., Monday's dinner)."""
    
    __tablename__ = "meal_plan_entries"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(String(64), nullable=False, index=True)
    date = Column(Date, nullable=False, index=True)
    meal_type = Column(String(20), nullable=False)  # breakfast, lunch, dinner, snack
    recipe_id = Column(UUID(as_uuid=True), ForeignKey("recipes.id", ondelete="CASCADE"), nullable=False)
    recipe_title = Column(String(255), nullable=False)  # Cached for quick display
    recipe_thumbnail = Column(String(500), nullable=True)  # Cached thumbnail URL
    notes = Column(String(500), nullable=True)  # Optional notes (e.g., "make extra for leftovers")
    servings = Column(String(20), nullable=True)  # Override servings if needed
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Composite unique constraint: one recipe per meal slot per day per user
    # (user can have different recipes for breakfast, lunch, dinner on same day)
    __table_args__ = (
        # No unique constraint - allow multiple recipes per meal slot if desired
        # Users might want 2 side dishes for dinner
        {},
    )

