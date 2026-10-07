"""
Database Engine & Session
=========================
Async SQLAlchemy engine and session factory for the application.
"""

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings

settings = get_settings()

# ── Engine ────────────────────────────────────────────────────────────────
engine = create_async_engine(
    settings.database_url,
    echo=settings.debug,
    future=True,
)

# ── Session factory ───────────────────────────────────────────────────────
async_session_factory = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


# ── Declarative base ─────────────────────────────────────────────────────
class Base(DeclarativeBase):
    """SQLAlchemy declarative base for all ORM models."""
    pass


# ── Dependency ────────────────────────────────────────────────────────────
async def get_db() -> AsyncSession:
    """FastAPI dependency that yields a database session.

    The session is automatically closed after the request finishes,
    ensuring proper connection pool management.
    """
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


# ── Table creation ────────────────────────────────────────────────────────
async def init_db() -> None:
    """Create all tables that don't yet exist."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
