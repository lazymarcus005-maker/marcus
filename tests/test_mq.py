import os
import uuid

import aio_pika
import pytest
import pytest_asyncio

from harness.mq import DLQ_NAME, declare_topology, decode_run_message, publish_run
from tests.fakes import get_message_with_wait

RABBITMQ_URL = os.environ.get("HARNESS_TEST_RABBITMQ_URL", "amqp://harness:harness@localhost:5672/")


@pytest_asyncio.fixture
async def channel():
    connection = await aio_pika.connect_robust(RABBITMQ_URL)
    async with connection:
        ch = await connection.channel()
        await declare_topology(ch)
        # Purge both queues so tests don't see leftovers from a previous run.
        main_queue = await ch.get_queue("agent.runs")
        await main_queue.purge()
        dlq = await ch.get_queue(DLQ_NAME)
        await dlq.purge()
        yield ch


@pytest.mark.asyncio
async def test_publish_and_consume_roundtrip(channel):
    run_id = uuid.uuid4()
    tenant_id = uuid.uuid4()

    await publish_run(channel, run_id, tenant_id)

    queue = await channel.get_queue("agent.runs")
    message = await get_message_with_wait(queue)
    body = decode_run_message(message.body)
    await message.ack()

    assert body["run_id"] == str(run_id)
    assert body["tenant_id"] == str(tenant_id)


@pytest.mark.asyncio
async def test_rejected_message_is_dead_lettered(channel):
    run_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    await publish_run(channel, run_id, tenant_id)

    queue = await channel.get_queue("agent.runs")
    message = await get_message_with_wait(queue)
    await message.reject(requeue=False)

    dlq = await channel.get_queue(DLQ_NAME)
    dead_message = await get_message_with_wait(dlq)
    body = decode_run_message(dead_message.body)
    await dead_message.ack()

    assert body["run_id"] == str(run_id)
