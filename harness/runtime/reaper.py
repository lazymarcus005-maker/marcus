import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from harness.db.enums import ApprovalStatus, RunStatus
from harness.db.models import AgentRun, ApprovalRequest
from harness.runtime.repository import RunRepository, StaleRunError

logger = logging.getLogger(__name__)

RequeueCallback = Callable[[AgentRun], Awaitable[None]]


async def reap_stale_runs(session: AsyncSession, on_stale: RequeueCallback) -> int:
    """Find Running runs whose lease has expired, clear the lease, and hand off to on_stale.

    A run only counts as stale if its worker stopped heartbeating — a live
    worker renews well before ttl via harness.runtime.lease.heartbeat_lease.
    Clearing the lease is itself gated on still owning it (release_lease uses
    the same version fencing as any other checkpoint), so if the original
    worker's heartbeat renews it a moment after this query ran, the release
    here loses the race and that run is correctly left alone.

    Returns the number of runs actually reaped.
    """
    result = await session.execute(
        sa.select(AgentRun).where(
            AgentRun.status == RunStatus.running,
            AgentRun.lease_owner.is_not(None),
            AgentRun.lease_expires_at < sa.func.now(),
        )
    )
    stale_runs = list(result.scalars().all())

    repo = RunRepository(session)
    reaped = 0
    for run in stale_runs:
        owner = run.lease_owner
        released = await repo.release_lease(run)
        if not released:
            continue  # lost the race to the original worker's heartbeat or another reaper
        logger.warning("reaped stale run %s (lease owner was %s)", run.id, owner)
        await on_stale(run)
        reaped += 1
    return reaped


async def reap_expired_approvals(session: AsyncSession) -> int:
    """Expire pending approvals past their deadline and time out their runs

    (decisions.md Q15 — issue #17). Unlike reap_stale_runs, an expired
    approval's run goes straight to a terminal status: there's nothing left
    to resume, so no requeue callback is needed.

    Returns the number of approvals actually expired.
    """
    result = await session.execute(
        sa.select(ApprovalRequest).where(
            ApprovalRequest.status == ApprovalStatus.pending,
            ApprovalRequest.expires_at < sa.func.now(),
        )
    )
    expired = list(result.scalars().all())

    repo = RunRepository(session)
    reaped = 0
    for approval in expired:
        approval.status = ApprovalStatus.expired
        approval.decided_at = datetime.now(UTC)
        await session.commit()

        run = await repo.get(approval.run_id)
        if run is None or run.status != RunStatus.waiting_approval:
            continue  # already moved on (race with a manual decision) — leave it alone
        try:
            await repo.checkpoint(
                run, status=RunStatus.timed_out, error="approval request expired"
            )
        except StaleRunError:
            continue  # lost the race between our check and the checkpoint
        await session.commit()
        logger.warning("expired approval %s; timed out run %s", approval.id, run.id)
        reaped += 1
    return reaped
