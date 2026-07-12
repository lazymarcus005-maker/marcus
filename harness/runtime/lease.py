import asyncio
import contextlib
import logging
import uuid

from sqlalchemy.ext.asyncio import async_sessionmaker

from harness.runtime.repository import RunRepository

logger = logging.getLogger(__name__)

HEARTBEAT_INTERVAL_FRACTION = 3  # renew every ttl/3 seconds


@contextlib.asynccontextmanager
async def heartbeat_lease(
    sessionmaker: async_sessionmaker, run_id: uuid.UUID, owner: str, ttl_seconds: int
):
    """Background task that renews a held lease periodically for the duration of the block.

    Runs on its own session (never the caller's) so it can safely operate
    concurrently with whatever DB work the caller does inside the block.
    Renewal is independent of the run's optimistic-locking version (see
    RunRepository.renew_lease_by_id) so the two don't fight over it.

    If renewal ever fails — meaning a reaper decided this worker was dead and
    handed the run to someone else — the caller's current task is cancelled so
    it stops making progress on a run it no longer owns.
    """
    owning_task = asyncio.current_task()
    assert owning_task is not None

    async def heartbeat() -> None:
        interval = max(ttl_seconds / HEARTBEAT_INTERVAL_FRACTION, 1)
        while True:
            await asyncio.sleep(interval)
            async with sessionmaker() as session:
                renewed = await RunRepository(session).renew_lease_by_id(run_id, owner, ttl_seconds)
            if not renewed:
                logger.warning(
                    "lease lost for run %s (owner=%s); cancelling worker task", run_id, owner
                )
                owning_task.cancel()
                return

    heartbeat_task = asyncio.create_task(heartbeat())
    try:
        yield
    finally:
        heartbeat_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat_task
