from functools import lru_cache
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

UNSUPPORTED_ASYNCPG_QUERY_PARAMS = frozenset({"sslmode", "channel_binding"})


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )
    
    # Database
    database_url: str
    database_use_ssl: bool = True
    
    # OpenAI
    openai_api_key: str

    # AI capability registry. Model IDs are pinned so a provider alias cannot
    # silently change behavior. Luna handles routine work; Terra is reserved
    # for deterministic fallback after a failed/invalid result.
    recipe_extraction_model: str = "gpt-5.6-luna"
    recipe_extraction_fallback_model: str = "gpt-5.6-terra"
    ocr_model: str = "gpt-5.6-luna"
    ocr_fallback_model: str = "gpt-5.6-terra"
    recipe_chat_model: str = "gpt-5.6-luna"
    cooking_chat_model: str = "gpt-5.6-luna"
    enrichment_model: str = "gpt-5.6-luna"
    transcription_model: str = "whisper-1"
    tts_model: str = "tts-1"
    openai_reasoning_effort: str = "none"
    ai_disabled_capabilities: str = ""
    
    # Clerk Auth
    clerk_secret_key: str | None = None
    clerk_secret_keys_by_issuer: str | None = None
    clerk_frontend_api: str = "clerk.your-domain.com"  # e.g., "prepared-mole-42.clerk.accounts.dev"
    clerk_jwt_issuer: str | None = None
    clerk_jwt_issuers: str | None = None
    clerk_jwt_audience: str | None = None
    clerk_migration_email_hash_secret: str | None = None
    
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
    
    # YouTube proxy (required for cloud hosting)
    # YouTube blocks datacenter IPs, so a residential proxy is needed
    # Format: http://username:password@p.webshare.io:80
    youtube_proxy: str | None = None
    video_download_timeout_seconds: int = 120
    video_metadata_timeout_seconds: int = 30
    video_max_duration_seconds: int = 3_600
    audio_max_bytes: int = 25 * 1024 * 1024

    # Durable database-backed extraction worker
    job_worker_enabled: bool = True
    job_worker_poll_seconds: float = 5.0
    job_lease_seconds: int = 600
    job_max_attempts: int = 3
    job_expiry_hours: int = 24
    
    # Sentry error monitoring
    sentry_dsn: str | None = None
    
    # Environment
    environment: str = "development"
    cors_origins: str = ""
    enable_sentry_debug: bool = False
    
    # API Settings
    api_title: str = "Recipe Extractor API"
    api_version: str = "1.0.0"

    @model_validator(mode="after")
    def validate_ai_registry(self) -> "Settings":
        """Reject missing, retired, or unsafe active model configuration."""
        configured_models = {
            "recipe_extraction": self.recipe_extraction_model,
            "recipe_extraction_fallback": self.recipe_extraction_fallback_model,
            "ocr": self.ocr_model,
            "ocr_fallback": self.ocr_fallback_model,
            "recipe_chat": self.recipe_chat_model,
            "cooking_chat": self.cooking_chat_model,
            "enrichment": self.enrichment_model,
            "transcription": self.transcription_model,
            "tts": self.tts_model,
        }
        for capability, model_id in configured_models.items():
            normalized = model_id.strip().lower()
            if not normalized:
                raise ValueError(f"{capability} model ID is required")
            if "gemini-2." in normalized or normalized.startswith("gpt-4o"):
                raise ValueError(f"{capability} uses a retired or deprecated model")

        if self.openai_reasoning_effort not in {"none", "low", "medium", "high", "xhigh"}:
            raise ValueError("OPENAI_REASONING_EFFORT must be none, low, medium, high, or xhigh")
        if self.job_worker_poll_seconds <= 0:
            raise ValueError("JOB_WORKER_POLL_SECONDS must be positive")
        if self.job_lease_seconds < 60:
            raise ValueError("JOB_LEASE_SECONDS must be at least 60")
        if self.job_max_attempts < 1:
            raise ValueError("JOB_MAX_ATTEMPTS must be at least 1")
        if self.job_expiry_hours < 1:
            raise ValueError("JOB_EXPIRY_HOURS must be at least 1")
        if self.environment.lower() != "development" and not self.database_use_ssl:
            raise ValueError("DATABASE_USE_SSL cannot be disabled outside development")
        return self

    @property
    def disabled_ai_capability_set(self) -> set[str]:
        """Capabilities disabled through the emergency runtime kill switch."""
        return {
            capability.strip().lower()
            for capability in self.ai_disabled_capabilities.split(",")
            if capability.strip()
        }

    def is_ai_capability_enabled(self, capability: str) -> bool:
        """Return whether a capability is allowed to call a paid provider."""
        disabled = self.disabled_ai_capability_set
        return "all" not in disabled and capability.lower() not in disabled
    
    @property
    def s3_enabled(self) -> bool:
        """Check if S3 is configured."""
        return all([
            self.aws_access_key_id,
            self.aws_secret_access_key,
            self.s3_bucket_name
        ])

    @property
    def clerk_issuer(self) -> str:
        """Expected Clerk JWT issuer."""
        if self.clerk_jwt_issuer:
            return self.clerk_jwt_issuer.rstrip("/")
        frontend_api = self.clerk_frontend_api.rstrip("/")
        if frontend_api.startswith("http://") or frontend_api.startswith("https://"):
            return frontend_api
        return f"https://{frontend_api}"

    @property
    def clerk_issuers(self) -> list[str]:
        """Allowed Clerk JWT issuers.

        During the Clerk production cutover, production temporarily accepts both
        the old Clerk development issuer and the new Clerk production issuer so
        existing App Store builds keep working while the new build rolls out.
        """
        if self.clerk_jwt_issuers:
            return [
                issuer.strip().rstrip("/")
                for issuer in self.clerk_jwt_issuers.split(",")
                if issuer.strip()
            ]
        return [self.clerk_issuer]

    @property
    def jwks_url(self) -> str:
        """Primary Clerk JWKS endpoint."""
        return f"{self.clerk_issuer}/.well-known/jwks.json"

    def jwks_url_for_issuer(self, issuer: str) -> str:
        """Clerk JWKS endpoint for a specific issuer."""
        return f"{issuer.rstrip('/')}/.well-known/jwks.json"

    @property
    def clerk_secret_key_by_issuer(self) -> dict[str, str]:
        """Map Clerk issuer URLs to Backend API secret keys.

        Format:
        CLERK_SECRET_KEYS_BY_ISSUER=https://old=sk_test_x,https://new=sk_live_y

        Use this during the Clerk production cutover so account deletion and
        email fallback call the matching Clerk instance for the verified token.
        """
        if not self.clerk_secret_keys_by_issuer:
            return {}

        mapping: dict[str, str] = {}
        for pair in self.clerk_secret_keys_by_issuer.split(","):
            if not pair.strip() or "=" not in pair:
                continue
            issuer, secret = pair.split("=", 1)
            issuer = issuer.strip().rstrip("/")
            secret = secret.strip()
            if issuer and secret:
                mapping[issuer] = secret
        return mapping

    def clerk_secret_key_for_issuer(self, issuer: str | None) -> str | None:
        """Return the Clerk secret key for a token issuer."""
        if issuer:
            issuer_secret = self.clerk_secret_key_by_issuer.get(issuer.rstrip("/"))
            if issuer_secret:
                return issuer_secret
        return self.clerk_secret_key

    @property
    def allowed_cors_origins(self) -> list[str]:
        """Allowed browser origins for CORS."""
        if self.cors_origins:
            return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

        if self.environment.lower() == "development":
            return ["*"]

        return [
            "https://hafa-recipes.com",
            "https://www.hafa-recipes.com",
        ]
    
    @property
    def async_database_url(self) -> str:
        """Convert database URL to async format for SQLAlchemy."""
        url = self.database_url
        # Convert to asyncpg driver
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql+asyncpg://", 1)
        elif url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
        # Remove libpq/psycopg SSL parameters that asyncpg does not accept as
        # keyword arguments. SSL is configured explicitly in app.db.database.
        parts = urlsplit(url)
        query_params = [
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if key.lower() not in UNSUPPORTED_ASYNCPG_QUERY_PARAMS
        ]
        return urlunsplit(parts._replace(query=urlencode(query_params)))


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()
