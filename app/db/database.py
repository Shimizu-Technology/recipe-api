import ssl
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import declarative_base
from app.config import get_settings

settings = get_settings()

# Create SSL context for Neon (required for secure connection)
ssl_context = ssl.create_default_context()
ssl_context.check_hostname = False
ssl_context.verify_mode = ssl.CERT_NONE

# Create async engine with SSL for Neon
engine = create_async_engine(
    settings.async_database_url,
    echo=settings.environment == "development",
    pool_pre_ping=True,
    connect_args={"ssl": ssl_context},
)

# Session factory
AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

# Base class for models
Base = declarative_base()


async def get_db():
    """Dependency that provides a database session."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()

