import uuid
from typing import Any

import aio_pika
import orjson

from harness.config import get_settings

EXCHANGE_NAME = "agent"
QUEUE_NAME = "agent.runs"
ROUTING_KEY = "run"
DLX_NAME = "agent.dlx"
DLQ_NAME = "agent.runs.dlq"


async def get_connection() -> aio_pika.abc.AbstractRobustConnection:
    settings = get_settings()
    return await aio_pika.connect_robust(settings.rabbitmq_url)


async def declare_topology(channel: aio_pika.abc.AbstractChannel) -> aio_pika.abc.AbstractQueue:
    """Declare the exchange/queue/DLQ topology. Idempotent — safe to call from

    both the API (to publish) and the worker (to consume).
    """
    dlx = await channel.declare_exchange(DLX_NAME, aio_pika.ExchangeType.DIRECT, durable=True)
    dlq = await channel.declare_queue(DLQ_NAME, durable=True)
    await dlq.bind(dlx, routing_key=ROUTING_KEY)

    exchange = await channel.declare_exchange(
        EXCHANGE_NAME, aio_pika.ExchangeType.DIRECT, durable=True
    )
    queue = await channel.declare_queue(
        QUEUE_NAME,
        durable=True,
        arguments={
            "x-dead-letter-exchange": DLX_NAME,
            "x-dead-letter-routing-key": ROUTING_KEY,
        },
    )
    await queue.bind(exchange, routing_key=ROUTING_KEY)
    return queue


def encode_run_message(run_id: uuid.UUID, tenant_id: uuid.UUID) -> bytes:
    return orjson.dumps({"run_id": str(run_id), "tenant_id": str(tenant_id)})


def decode_run_message(body: bytes) -> dict[str, Any]:
    return orjson.loads(body)


async def publish_run(
    channel: aio_pika.abc.AbstractChannel, run_id: uuid.UUID, tenant_id: uuid.UUID
) -> None:
    exchange = await channel.declare_exchange(
        EXCHANGE_NAME, aio_pika.ExchangeType.DIRECT, durable=True
    )
    await exchange.publish(
        aio_pika.Message(
            body=encode_run_message(run_id, tenant_id),
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
        ),
        routing_key=ROUTING_KEY,
    )


async def publish_run_standalone(run_id: uuid.UUID, tenant_id: uuid.UUID) -> None:
    """Open a connection, publish one message, close. Used by the API layer,

    which doesn't keep a long-lived broker connection around (see
    harness/api/app.py) — simple and robust for MVP request volumes over a
    pooled/persistent connection.
    """
    connection = await get_connection()
    async with connection:
        channel = await connection.channel()
        await declare_topology(channel)  # idempotent; guards against a cold broker
        await publish_run(channel, run_id, tenant_id)
