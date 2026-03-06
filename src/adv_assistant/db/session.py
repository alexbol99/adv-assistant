import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

DEFAULT_DATABASE_URL = "sqlite+aiosqlite:///./adv_assistant.db"


def get_database_url() -> str:
    return os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL)


def _is_sqlite_url(url: str) -> bool:
    return url.startswith("sqlite")


def create_engine(database_url: str | None = None, *, echo: bool = False) -> AsyncEngine:
    url = database_url or get_database_url()
    kwargs: dict = {
        "echo": echo,
        "pool_pre_ping": True,
    }
    if _is_sqlite_url(url):
        # Give SQLite 30 s to wait for a locked database instead of failing
        # immediately.  This is the *connection-level* busy timeout.
        kwargs["connect_args"] = {"timeout": 30}

    engine = create_async_engine(url, **kwargs)

    if _is_sqlite_url(url):
        # Enable WAL journal mode so readers never block writers and vice-versa.
        @event.listens_for(engine.sync_engine, "connect")
        def _set_sqlite_pragma(dbapi_connection, _connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.close()

    return engine


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


@asynccontextmanager
async def session_scope(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    session = session_factory()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()
