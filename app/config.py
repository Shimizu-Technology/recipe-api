from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""
    
    # Database
    database_url: str
    
    # OpenAI
    openai_api_key: str
    
    # Optional
    ig_oembed_token: str | None = None
    
    # Environment
    environment: str = "development"
    
    # API Settings
    api_title: str = "Recipe Extractor API"
    api_version: str = "1.0.0"
    
    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"
    
    @property
    def async_database_url(self) -> str:
        """Convert database URL to async format for SQLAlchemy."""
        url = self.database_url
        # Convert to asyncpg driver
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql+asyncpg://", 1)
        elif url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
        # Remove sslmode parameter (handled separately by asyncpg)
        if "?sslmode=" in url:
            url = url.split("?sslmode=")[0]
        elif "&sslmode=" in url:
            url = url.replace("&sslmode=require", "").replace("&sslmode=prefer", "")
        return url


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()

