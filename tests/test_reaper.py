import uuid
from datetime import UTC, datetime, timedelta

import pytest

from harness.db.enums import MessageRole, RunStatus
from harness.db.models import Tenant
from harness.runtime.reaper import reap_stale_runs
from harness.runtime.repository import RunRepository


async def _make_running_leased_run(session, *, lease_expires_at):
    tenant = Tenant(name=f"t-{uuid.uuid4()}")
    session.add(tenant)
    await session.flush()
    repo = RunRepository(session)
    run = await repo.create_run(tenant_id=tenant.id, goal="goal")
    await repo.add_message(run.id, MessageRole.user, "goal")
    run = await repo.checkpoint(run, status=RunStatus.running)
    run.lease_owner = "dead-worker"
    run.lease_expires_at = lease_expires_at
    await session.flush()
    await session.commit()
    return run


@pytest.mark.asyncio
async def test_reap_stale_runs_finds_expired_lease_and_clears_it(db_session):
    run = await _make_running_leased_run(
        db_session, lease_expires_at=datetime.now(UTC) - timedelta(seconds=5)
    )

    reaped_runs = []

    async def on_stale(reaped_run):
        reaped_runs.append(reaped_run.id)

    count = await reap_stale_runs(db_session, on_stale)

    assert count == 1
    assert reaped_runs == [run.id]

    repo = RunRepository(db_session)
    refreshed = await repo.get(run.id)
    assert refreshed.lease_owner is None
    assert refreshed.status == RunStatus.running  # reaper only clears the lease, not the status


@pytest.mark.asyncio
async def test_reap_stale_runs_ignores_active_leases(db_session):
    await _make_running_leased_run(
        db_session, lease_expires_at=datetime.now(UTC) + timedelta(minutes=5)
    )

    calls = []

    async def on_stale(run):
        calls.append(run.id)

    count = await reap_stale_runs(db_session, on_stale)

    assert count == 0
    assert calls == []


@pytest.mark.asyncio
async def test_reap_stale_runs_ignores_unleased_or_non_running(db_session):
    tenant = Tenant(name=f"t-{uuid.uuid4()}")
    db_session.add(tenant)
    await db_session.flush()
    repo = RunRepository(db_session)
    run = await repo.create_run(tenant_id=tenant.id, goal="goal")  # status=pending, no lease
    await db_session.commit()

    calls = []

    async def on_stale(r):
        calls.append(r.id)

    count = await reap_stale_runs(db_session, on_stale)

    assert count == 0
    assert calls == []
    assert run.id not in calls
