import asyncio
import uuid

import pytest

from harness.db.models import Tenant
from harness.runtime.lease import heartbeat_lease
from harness.runtime.repository import RunRepository


async def _make_run(db_sessionmaker):
    async with db_sessionmaker() as session:
        tenant = Tenant(name=f"t-{uuid.uuid4()}")
        session.add(tenant)
        await session.flush()
        repo = RunRepository(session)
        run = await repo.create_run(tenant_id=tenant.id, goal="goal")
        acquired = await repo.try_acquire_lease(run, "worker-a", ttl_seconds=1)
        assert acquired
        await session.commit()
        return run.id


@pytest.mark.asyncio
async def test_heartbeat_renews_lease_while_held(db_sessionmaker):
    run_id = await _make_run(db_sessionmaker)

    async with heartbeat_lease(db_sessionmaker, run_id, "worker-a", ttl_seconds=1):
        await asyncio.sleep(0.6)  # > ttl/3 heartbeat interval (~0.33s)

        async with db_sessionmaker() as session:
            run = await RunRepository(session).get(run_id)
            assert run.lease_owner == "worker-a"


@pytest.mark.asyncio
async def test_heartbeat_cancels_owner_when_lease_is_lost(db_sessionmaker):
    run_id = await _make_run(db_sessionmaker)

    async def hold_lease_and_wait():
        async with heartbeat_lease(db_sessionmaker, run_id, "worker-a", ttl_seconds=1):
            await asyncio.sleep(5)  # would only complete if never cancelled

    task = asyncio.create_task(hold_lease_and_wait())
    await asyncio.sleep(0.1)  # let the heartbeat task start

    # Simulate a reaper handing the lease to someone else mid-flight.
    async with db_sessionmaker() as session:
        run = await RunRepository(session).get(run_id)
        run.lease_owner = "worker-b"
        await session.commit()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=3)
