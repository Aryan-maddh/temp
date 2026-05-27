import os
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

ROOT_DIR: Path = Path(__file__).resolve().parents[1]

# Look for .env in: src/app/core/.env → project root backend/.env → project root .env
_env_candidates = [
    Path(__file__).parent / ".env",
    Path(__file__).resolve().parents[3] / "backend" / ".env",
    Path(__file__).resolve().parents[3] / ".env",
]
for _env_path in _env_candidates:
    if _env_path.exists():
        load_dotenv(_env_path)
        break

DATABASE_URL: str = os.environ["DATABASE_URL"]

engine = create_async_engine(DATABASE_URL, echo=False, pool_pre_ping=True)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)

# Compatibility aliases for the older, proven application-filling engine.
SessionLocal = AsyncSessionLocal


class Base(DeclarativeBase):
    pass


async def get_db():
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_session():
    async with SessionLocal() as session:
        yield session


async def init_db() -> None:
    from app.core import models  # noqa: F401 — registers all ORM models with Base.metadata

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)