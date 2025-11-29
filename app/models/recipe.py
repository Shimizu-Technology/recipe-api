"""SQLAlchemy models matching existing Drizzle schema in Neon database."""

from sqlalchemy import Column, String, Text, Boolean, Integer, DateTime, ForeignKey
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
import uuid

from app.db.database import Base


class Recipe(Base):
    """
    Recipe model - matches existing 'recipes' table in Neon.
    
    The 'extracted' JSONB column contains the full recipe data including:
    - title, servings, times
    - components (new multi-component structure)
    - ingredients, steps (legacy fields)
    - equipment, notes, tags
    - nutrition, cost info
    """
    __tablename__ = "recipes"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_url = Column(Text, nullable=False)
    source_type = Column(String(32), nullable=False)  # youtube|tiktok|instagram|web|manual
    raw_text = Column(Text, nullable=True)
    extracted = Column(JSONB, nullable=False)
    thumbnail_url = Column(Text, nullable=True)
    extraction_method = Column(String(32), nullable=True)  # whisper|basic|oembed|manual
    extraction_quality = Column(String(16), nullable=True)  # high|medium|low
    has_audio_transcript = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    
    # User ownership (Clerk user ID) - nullable for legacy recipes
    user_id = Column(String(64), nullable=True, index=True)
    
    # Public visibility - True means visible in Discover feed
    # Legacy recipes (user_id=NULL) are public by default
    is_public = Column(Boolean, nullable=False, default=False, server_default="false")
    
    # Relationship to extraction jobs
    extraction_jobs = relationship("ExtractionJob", back_populates="recipe")
    
    def __repr__(self):
        title = self.extracted.get("title", "Untitled") if self.extracted else "Untitled"
        return f"<Recipe {self.id}: {title}>"


class ExtractionJob(Base):
    """
    Extraction job model - matches existing 'extraction_jobs' table in Neon.
    
    Tracks the progress of recipe extraction from video URLs.
    """
    __tablename__ = "extraction_jobs"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    url = Column(Text, nullable=False, unique=True)
    location = Column(Text, nullable=False, default="Guam")
    notes = Column(Text, nullable=False, default="")
    status = Column(String(16), nullable=False, default="processing")  # processing|completed|failed
    progress = Column(Integer, nullable=False, default=0)  # 0-100
    current_step = Column(String(32), nullable=False, default="initializing")
    message = Column(Text, nullable=False, default="Starting extraction...")
    estimated_duration = Column(Integer, nullable=False, default=60)  # seconds
    recipe_id = Column(UUID(as_uuid=True), ForeignKey("recipes.id"), nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    
    # Relationship to recipe
    recipe = relationship("Recipe", back_populates="extraction_jobs")
    
    def __repr__(self):
        return f"<ExtractionJob {self.id}: {self.status}>"

