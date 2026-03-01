from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from adv_assistant.db.base import Base
from adv_assistant.db.session import create_engine


@pytest.fixture()
async def db_session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    db_path = tmp_path / "phase1.db"
    engine = create_engine(f"sqlite+aiosqlite:///{db_path}")

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    async with session_factory() as session:
        yield session

    await engine.dispose()
