import asyncio
import logging
import os
import socket
from datetime import UTC, datetime

import redis.asyncio as redis
import sqlalchemy as sa
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.ext.asyncio import AsyncSession

from harness.config import Settings, get_settings
from harness.db.enums import ScheduledJobStatus
from harness.db.models import ScheduledJob
from harness.db.session import get_sessionmaker
from harness.mq import publish_run_standalone
from harness.observability import configure_logging, span
from harness.runtime.quotas import enforce_tenant_run_quota
from harness.runtime.repository import RunRepository

logger = logging.getLogger("harness.scheduler")
SCHEDULER_ID = f"{socket.gethostname()}-{os.getpid()}"


def validate_cron(expression: str) -> CronTrigger:
    parts = expression.split()
    if len(parts) != 5:
        raise ValueError("cron expression must have 5 fields")
    return CronTrigger.from_crontab(expression, timezone=UTC)


def next_run_time(expression: str, now: datetime | None = None) -> datetime | None:
    trigger = validate_cron(expression)
    return trigger.get_next_fire_time(None, now or datetime.now(UTC))


async def refresh_job_next_run(job: ScheduledJob) -> None:
    job.next_run_at = next_run_time(job.cron_expression) if job.enabled else None
    job.status = ScheduledJobStatus.idle if job.enabled else ScheduledJobStatus.disabled


async def list_due_jobs(session: AsyncSession, now: datetime) -> list[ScheduledJob]:
    result = await session.execute(
        sa.select(ScheduledJob)
        .where(ScheduledJob.enabled.is_(True), ScheduledJob.next_run_at <= now)
        .order_by(ScheduledJob.next_run_at)
    )
    return list(result.scalars().all())


async def fire_scheduled_job(
    session: AsyncSession,
    job: ScheduledJob,
    *,
    settings: Settings,
    publisher=publish_run_standalone,
):
    with span("scheduler.fire_job", tenant_id=str(job.tenant_id), scheduled_job_id=str(job.id)):
        await enforce_tenant_run_quota(session, tenant_id=job.tenant_id, settings=settings)
        repo = RunRepository(session)
        run = await repo.create_run(
            tenant_id=job.tenant_id,
            goal=job.goal,
            channel=job.channel,
            channel_metadata={
                **job.channel_metadata,
                "scheduled_job_id": str(job.id),
                "scheduled_job_name": job.name,
            },
        )
        job.status = ScheduledJobStatus.firing
        job.last_run_at = datetime.now(UTC)
        job.last_run_id = run.id
        job.next_run_at = next_run_time(job.cron_expression, job.last_run_at)
        await session.commit()
        try:
            await publisher(run.id, job.tenant_id)
        except Exception:
            job.status = ScheduledJobStatus.idle if job.enabled else ScheduledJobStatus.disabled
            await session.commit()
            raise
        job.status = ScheduledJobStatus.idle if job.enabled else ScheduledJobStatus.disabled
        await session.commit()
        return run


async def acquire_scheduler_leader(settings: Settings) -> tuple[redis.Redis, bool]:
    client = redis.from_url(settings.redis_url, decode_responses=True)
    ok = await client.set(
        "scheduler:leader", SCHEDULER_ID, nx=True, ex=settings.scheduler_lease_ttl_seconds
    )
    return client, bool(ok)


async def scheduler_loop(settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    configure_logging(settings)
    sessionmaker = get_sessionmaker()
    logger.info("scheduler starting", extra={"status": "starting"})
    while True:
        client, acquired = await acquire_scheduler_leader(settings)
        try:
            if acquired:
                async with sessionmaker() as session:
                    now = datetime.now(UTC)
                    for job in await list_due_jobs(session, now):
                        try:
                            await fire_scheduled_job(session, job, settings=settings)
                        except Exception:
                            logger.exception(
                                "failed to fire scheduled job",
                                extra={
                                    "scheduled_job_id": str(job.id),
                                    "tenant_id": str(job.tenant_id),
                                },
                            )
            await asyncio.sleep(settings.scheduler_poll_seconds)
        finally:
            await client.aclose()


async def main() -> None:
    await scheduler_loop()


if __name__ == "__main__":
    asyncio.run(main())
