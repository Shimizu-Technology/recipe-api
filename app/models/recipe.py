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
    original_extracted = Column(JSONB, nullable=True)  # Stores original AI extraction before user edits
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


class SavedRecipe(Base):
    """
    SavedRecipe model - tracks which recipes users have bookmarked/saved.
    
    Allows users to save public recipes from other users to their collection.
    """
    __tablename__ = "saved_recipes"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(String(64), nullable=False, index=True)
    recipe_id = Column(UUID(as_uuid=True), ForeignKey("recipes.id", ondelete="CASCADE"), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    
    # Relationship to recipe
    recipe = relationship("Recipe")
    
    def __repr__(self):
        return f"<SavedRecipe user={self.user_id} recipe={self.recipe_id}>"


class Collection(Base):
    """
    Collection model - user-created folders for organizing recipes.
    
    Users can create collections like "Weeknight Dinners", "Holiday Favorites", etc.
    """
    __tablename__ = "collections"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(String(64), nullable=False, index=True)
    name = Column(String(100), nullable=False)
    emoji = Column(String(10), nullable=True)  # Optional emoji icon
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    
    # Relationship to recipes via junction table
    recipes = relationship("Recipe", secondary="collection_recipes", backref="collections")
    
    def __repr__(self):
        return f"<Collection {self.id}: {self.name}>"


class CollectionRecipe(Base):
    """
    CollectionRecipe model - junction table for many-to-many relationship
    between collections and recipes.
    """
    __tablename__ = "collection_recipes"
    
    collection_id = Column(UUID(as_uuid=True), ForeignKey("collections.id", ondelete="CASCADE"), primary_key=True)
    recipe_id = Column(UUID(as_uuid=True), ForeignKey("recipes.id", ondelete="CASCADE"), primary_key=True)
    added_at = Column(DateTime(timezone=True), server_default=func.now())
    
    def __repr__(self):
        return f"<CollectionRecipe collection={self.collection_id} recipe={self.recipe_id}>"


class RecipeNote(Base):
    """
    RecipeNote model - user's private notes on any recipe.
    
    Allows users to add personal notes to any recipe (their own or saved from others).
    Notes are private - only visible to the user who created them.
    """
    __tablename__ = "recipe_notes"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(String(64), nullable=False, index=True)
    recipe_id = Column(UUID(as_uuid=True), ForeignKey("recipes.id", ondelete="CASCADE"), nullable=False, index=True)
    note_text = Column(Text, nullable=False, default="")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    
    # Relationship to recipe
    recipe = relationship("Recipe")
    
    def __repr__(self):
        return f"<RecipeNote user={self.user_id} recipe={self.recipe_id}>"


class RecipeVersion(Base):
    """
    RecipeVersion model - tracks all versions of a recipe.
    
    Stores snapshots of the recipe data whenever it's edited or re-extracted.
    Allows users to view history and restore to any previous version.
    """
    __tablename__ = "recipe_versions"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    recipe_id = Column(UUID(as_uuid=True), ForeignKey("recipes.id", ondelete="CASCADE"), nullable=False, index=True)
    version_number = Column(Integer, nullable=False)
    extracted = Column(JSONB, nullable=False)  # Snapshot of recipe data
    thumbnail_url = Column(Text, nullable=True)
    change_type = Column(String(32), nullable=False, default="edit")  # initial, edit, re-extract
    change_summary = Column(Text, nullable=True)  # Optional description of changes
    created_by = Column(String(64), nullable=True)  # User who made the change
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    
    # Relationship to recipe
    recipe = relationship("Recipe", backref="versions")
    
    def __repr__(self):
        return f"<RecipeVersion recipe={self.recipe_id} v{self.version_number}>"


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

