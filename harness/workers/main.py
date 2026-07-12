import asyncio
import logging
import os
import socket
import uuid

import aio_pika
from opentelemetry import context as otel_context
from opentelemetry.propagate import extract
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from harness.config import Settings, get_settings
from harness.db.models import TenantLlmSetting
from harness.db.session import get_sessionmaker
from harness.llm.gateway import LLMGateway
from harness.mcp.crypto import decrypt
from harness.mq import declare_topology, decode_run_message, get_connection, publish_run
from harness.observability import configure_logging, span
from harness.runtime.engine import RunEngine
from harness.runtime.lease import heartbeat_lease
from harness.runtime.reaper import reap_expired_approvals, reap_stale_runs
from harness.runtime.repository import RunRepository
from harness.slack.service import post_run_update_to_slack

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("harness.worker")

WORKER_ID = f"{socket.gethostname()}-{os.getpid()}"


async def _resolve_llm_gateway(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    default_llm: LLMGateway,
    settings: Settings,
) -> tuple[LLMGateway, bool]:
    """Return the LLMGateway to use for this tenant's run.

    Falls back to the shared process-wide gateway (env-var Settings) when the
    tenant hasn't configured its own provider via /v1/llm-settings. The bool
    tells the caller whether it owns (and must close) the returned gateway.
    """
    tenant_setting = await session.get(TenantLlmSetting, tenant_id)
    if tenant_setting is None:
        return default_llm, False

    effective_settings = settings.model_copy(
        update={
            "llm_base_url": tenant_setting.base_url,
            "llm_api_key": decrypt(tenant_setting.api_key_encrypted),
            "llm_model": tenant_setting.model,
        }
    )
    return LLMGateway(settings=effective_settings), True


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
    ctx = extract(message.headers or {})
    token = otel_context.attach(ctx)
    try:
        async with message.process(requeue=False, reject_on_redelivered=True):
            await _process_message_inner(message, sessionmaker, llm, settings)
    finally:
        otel_context.detach(token)


async def _process_message_inner(
    message: aio_pika.abc.AbstractIncomingMessage,
    sessionmaker: async_sessionmaker,
    llm: LLMGateway,
    settings: Settings,
) -> None:
    body = decode_run_message(message.body)
    run_id = uuid.UUID(body["run_id"])
    with span("agent.worker.consume", run_id=str(run_id), tenant_id=body.get("tenant_id")):
        async with sessionmaker() as claim_session:
            repo = RunRepository(claim_session)
            run = await repo.get(run_id)
            if run is None:
                logger.warning(
                    "run %s not found; dropping message", run_id, extra={"run_id": run_id}
                )
                return
            acquired = await repo.try_acquire_lease(run, WORKER_ID, settings.run_lease_ttl_seconds)
            await claim_session.commit()

        if not acquired:
            logger.info(
                "run %s already leased by another worker; skipping", run_id, extra={"run_id": run_id}
            )
            return

        try:
            async with (
                heartbeat_lease(sessionmaker, run_id, WORKER_ID, settings.run_lease_ttl_seconds),
                sessionmaker() as session,
            ):
                tenant_id = uuid.UUID(body["tenant_id"])
                llm_for_run, owns_llm = await _resolve_llm_gateway(
                    session, tenant_id=tenant_id, default_llm=llm, settings=settings
                )
                try:
                    engine = RunEngine(session, llm_for_run)
                    final = await engine.run_until_blocked(run_id)
                    try:
                        await post_run_update_to_slack(session, run=final, settings=settings)
                    except Exception:
                        # The run already completed and is persisted; a Slack
                        # notification failure must not dead-letter this message.
                        logger.exception(
                            "failed to post Slack update for completed run",
                            extra={"run_id": str(run_id)},
                        )
                finally:
                    if owns_llm:
                        await llm_for_run.aclose()
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
                    logger.exception("stale-run reaper iteration failed")
            async with sessionmaker() as session:
                try:
                    await reap_expired_approvals(session)
                except Exception:
                    logger.exception("expired-approval reaper iteration failed")


async def main() -> None:
    settings = get_settings()
    configure_logging(settings)
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
