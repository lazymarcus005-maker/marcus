import uuid
from datetime import UTC, datetime, timedelta

import pytest

from harness.db.enums import ApprovalStatus, MessageRole, RiskTier, RunStatus
from harness.db.models import ApprovalRequest, Tenant
from harness.runtime.reaper import reap_expired_approvals, reap_stale_runs
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


async def _make_waiting_approval_run(session, *, expires_at):
    tenant = Tenant(name=f"t-{uuid.uuid4()}")
    session.add(tenant)
    await session.flush()
    repo = RunRepository(session)
    run = await repo.create_run(tenant_id=tenant.id, goal="delete something")
    await repo.add_message(run.id, MessageRole.user, "delete something")
    run = await repo.checkpoint(run, status=RunStatus.running)
    run = await repo.checkpoint(run, status=RunStatus.waiting_approval)
    approval = ApprovalRequest(
        tenant_id=tenant.id,
        run_id=run.id,
        step_no=0,
        call_index=0,
        tool_name="delete_resource",
        risk_tier=RiskTier.destructive,
        args={"id": "abc"},
        status=ApprovalStatus.pending,
        expires_at=expires_at,
    )
    session.add(approval)
    await session.commit()
    return run, approval


@pytest.mark.asyncio
async def test_reap_expired_approvals_times_out_the_run(db_session):
    run, approval = await _make_waiting_approval_run(
        db_session, expires_at=datetime.now(UTC) - timedelta(hours=1)
    )

    count = await reap_expired_approvals(db_session)

    assert count == 1
    await db_session.refresh(approval)
    assert approval.status == ApprovalStatus.expired
    assert approval.decided_at is not None

    repo = RunRepository(db_session)
    refreshed = await repo.get(run.id)
    assert refreshed.status == RunStatus.timed_out
    assert "approval" in refreshed.error


@pytest.mark.asyncio
async def test_reap_expired_approvals_ignores_future_expiry(db_session):
    await _make_waiting_approval_run(db_session, expires_at=datetime.now(UTC) + timedelta(hours=1))

    count = await reap_expired_approvals(db_session)

    assert count == 0


@pytest.mark.asyncio
async def test_reap_expired_approvals_skips_run_no_longer_waiting(db_session):
    run, approval = await _make_waiting_approval_run(
        db_session, expires_at=datetime.now(UTC) - timedelta(hours=1)
    )
    repo = RunRepository(db_session)
    run = await repo.get(run.id)
    # Simulate a manual decision beating the reaper to it.
    await repo.checkpoint(run, status=RunStatus.running)
    await db_session.commit()

    count = await reap_expired_approvals(db_session)

    # The approval itself still expires (it was never actually decided)...
    assert count == 0
    await db_session.refresh(approval)
    assert approval.status == ApprovalStatus.expired
    # ...but the run, no longer waiting_approval, is left alone.
    refreshed = await repo.get(run.id)
    assert refreshed.status == RunStatus.running
