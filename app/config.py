from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""
    
    # Database
    database_url: str
    
    # OpenAI
    openai_api_key: str
    
    # OpenRouter (optional - for model benchmarking/switching)
    openrouter_api_key: str | None = None
    
    # Clerk Auth
    clerk_secret_key: str | None = None
    clerk_frontend_api: str = "clerk.your-domain.com"  # e.g., "prepared-mole-42.clerk.accounts.dev"
    
    # AWS S3 (for thumbnail storage)
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None
    aws_region: str = "us-east-1"
    s3_bucket_name: str | None = None
    
    # Optional
    ig_oembed_token: str | None = None
    
    # Instagram cookies (for yt-dlp authentication)
    # Can be either a file path or the raw cookie content
    instagram_cookies: str | None = None
    
    # Sentry error monitoring
    sentry_dsn: str | None = None
    
    # Environment
    environment: str = "development"
    
    # API Settings
    api_title: str = "Recipe Extractor API"
    api_version: str = "1.0.0"
    
    @property
    def s3_enabled(self) -> bool:
        """Check if S3 is configured."""
        return all([
            self.aws_access_key_id,
            self.aws_secret_access_key,
            self.s3_bucket_name
        ])
    
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

