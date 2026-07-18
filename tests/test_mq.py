import asyncio
import os
import uuid

import aio_pika
import pytest
import pytest_asyncio

from harness.mq import DLQ_NAME, declare_topology, decode_run_message, publish_run
from tests.fakes import get_message_with_wait

RABBITMQ_URL = os.environ.get("HARNESS_TEST_RABBITMQ_URL", "amqp://harness:harness@localhost:5672/")


def _rabbitmq_available() -> bool:
    """Return True if the configured RabbitMQ broker is reachable."""
    url = RABBITMQ_URL
    if os.environ.get("HARNESS_RUN_MQ_TESTS") != "1":
        return False
    try:
        import urllib.parse

        parsed = urllib.parse.urlparse(url)
        host = parsed.hostname or "localhost"
        port = parsed.port or 5672
        with asyncio.runners.Runner() as runner:
            return runner.run(_can_connect(host, port))
    except Exception:
        return False


async def _can_connect(host: str, port: int) -> bool:
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=2)
        writer.close()
        await writer.wait_closed()
        return True
    except Exception:
        return False


@pytest.fixture(scope="module")
def mq_available() -> bool:
    return _rabbitmq_available()


@pytest_asyncio.fixture
async def channel(mq_available: bool):
    if not mq_available:
        pytest.skip("RabbitMQ is not reachable; set HARNESS_RUN_MQ_TESTS=1 to run MQ tests")
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
