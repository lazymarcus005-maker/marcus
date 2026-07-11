import asyncio
import logging
import os
import socket
import uuid

import aio_pika
from sqlalchemy.ext.asyncio import async_sessionmaker

from harness.config import Settings, get_settings
from harness.db.session import get_sessionmaker
from harness.llm.gateway import LLMGateway
from harness.mq import declare_topology, decode_run_message, get_connection, publish_run
from harness.runtime.engine import RunEngine
from harness.runtime.lease import heartbeat_lease
from harness.runtime.reaper import reap_stale_runs
from harness.runtime.repository import RunRepository

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("harness.worker")

WORKER_ID = f"{socket.gethostname()}-{os.getpid()}"


async def process_message(
    message: aio_pika.abc.AbstractIncomingMessage,
    sessionmaker: async_sessionmaker,
    llm: LLMGateway,
    settings: Settings,
) -> None:
    """Handle one queued run: acquire its lease, run the engine, release the lease.

    Acks on success; on any unhandled exception, nacks without requeue so the
    message dead-letters (see harness.mq's DLQ topology) instead of looping.
    """
    async with message.process(requeue=False, reject_on_redelivered=True):
        body = decode_run_message(message.body)
        run_id = uuid.UUID(body["run_id"])

        async with sessionmaker() as claim_session:
            repo = RunRepository(claim_session)
            run = await repo.get(run_id)
            if run is None:
                logger.warning("run %s not found; dropping message", run_id)
                return
            acquired = await repo.try_acquire_lease(run, WORKER_ID, settings.run_lease_ttl_seconds)
            await claim_session.commit()

        if not acquired:
            logger.info("run %s already leased by another worker; skipping", run_id)
            return

        try:
            async with (
                heartbeat_lease(sessionmaker, run_id, WORKER_ID, settings.run_lease_ttl_seconds),
                sessionmaker() as session,
            ):
                engine = RunEngine(session, llm)
                await engine.run_until_blocked(run_id)
        finally:
            async with sessionmaker() as release_session:
                repo = RunRepository(release_session)
                current = await repo.get(run_id)
                if current is not None and current.lease_owner == WORKER_ID:
                    await repo.release_lease(current)
                    await release_session.commit()


async def consume_loop(
    sessionmaker: async_sessionmaker, llm: LLMGateway, settings: Settings
) -> None:
    connection = await get_connection()
    async with connection:
        channel = await connection.channel()
        await channel.set_qos(prefetch_count=4)
        queue = await declare_topology(channel)

        logger.info("worker %s consuming from %s", WORKER_ID, queue.name)
        async with queue.iterator() as queue_iter:
            async for message in queue_iter:
                try:
                    await process_message(message, sessionmaker, llm, settings)
                except Exception:
                    logger.exception("unhandled error processing message; dead-lettered")


async def reaper_loop(sessionmaker: async_sessionmaker, settings: Settings) -> None:
    connection = await get_connection()
    async with connection:
        channel = await connection.channel()
        await declare_topology(channel)

        async def on_stale(run) -> None:
            await publish_run(channel, run.id, run.tenant_id)

        while True:
            await asyncio.sleep(settings.run_lease_ttl_seconds)
            async with sessionmaker() as session:
                try:
                    await reap_stale_runs(session, on_stale)
                except Exception:
                    logger.exception("reaper iteration failed")


async def main() -> None:
    settings = get_settings()
    sessionmaker = get_sessionmaker()
    llm = LLMGateway(settings=settings)
    logger.info("worker starting", extra={"env": settings.env, "worker_id": WORKER_ID})

    try:
        await asyncio.gather(
            consume_loop(sessionmaker, llm, settings), reaper_loop(sessionmaker, settings)
        )
    finally:
        await llm.aclose()


if __name__ == "__main__":
    asyncio.run(main())
