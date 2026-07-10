import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from harness.db.base import Base

TEST_DATABASE_URL = os.environ.get(
    "HARNESS_TEST_DATABASE_URL",
    "postgresql+asyncpg://harness:harness@localhost:5432/harness_test",
)


@pytest_asyncio.fixture
async def db_session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(TEST_DATABASE_URL)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    async with sessionmaker() as session:
        yield session

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


def pytest_collection_modifyitems(config, items):
    if os.environ.get("HARNESS_SKIP_DB_TESTS") == "1":
        skip_db = pytest.mark.skip(reason="HARNESS_SKIP_DB_TESTS=1")
        for item in items:
            if "db_session" in item.fixturenames:
                item.add_marker(skip_db)
