"""Async SQLAlchemy database lifecycle for durable task facts."""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from athena.api.repositories.models import Base
from athena.config import DatabaseSettings


def _async_url(url: str) -> str:
    if url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + url.removeprefix("postgresql://")
    if url.startswith("sqlite://"):
        return "sqlite+aiosqlite://" + url.removeprefix("sqlite://")
    return url


class Database:
    """Own one async engine and a transaction-scoped session factory."""

    def __init__(self, settings: DatabaseSettings) -> None:
        if not settings.url:
            raise ValueError("database URL is required")
        url = _async_url(settings.url)
        options: dict[str, object] = {"echo": settings.echo, "pool_pre_ping": True}
        if url.startswith("sqlite+aiosqlite:///:memory:"):
            options["poolclass"] = StaticPool
            options["connect_args"] = {"check_same_thread": False}
        elif not url.startswith("sqlite+"):
            options["pool_size"] = settings.pool_size
            options["max_overflow"] = settings.max_overflow
        self.engine: AsyncEngine = create_async_engine(url, **options)
        self.session_factory = async_sessionmaker(
            self.engine, expire_on_commit=False, class_=AsyncSession
        )

    async def create_schema(self) -> None:
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def drop_schema(self) -> None:
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)

    async def dispose(self) -> None:
        await self.engine.dispose()

    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.session_factory() as session:
            yield session
