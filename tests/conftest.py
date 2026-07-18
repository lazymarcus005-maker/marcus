import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from harness.db.base import Base

TEST_DATABASE_URL = os.environ.get(
    "HARNESS_TEST_DATABASE_URL",
    "postgresql+asyncpg://harness:harness@localhost:5432/harness_test",
)


@pytest_asyncio.fixture
async def db_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(TEST_DATABASE_URL)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield engine

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def db_sessionmaker(db_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(db_engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def db_session(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with db_sessionmaker() as session:
        yield session


_DB_FIXTURES = {"db_engine", "db_sessionmaker", "db_session"}


def pytest_collection_modifyitems(config, items):
    if (
        os.environ.get("HARNESS_SKIP_DB_TESTS") == "1"
        or os.environ.get("HARNESS_RUN_DB_TESTS") != "1"
    ):
        skip_db = pytest.mark.skip(
            reason="set HARNESS_RUN_DB_TESTS=1 to run PostgreSQL integration tests"
        )
        for item in items:
            if _DB_FIXTURES & set(item.fixturenames):
                item.add_marker(skip_db)
