import os
import uuid

import aio_pika
import pytest
import pytest_asyncio

from harness.config import get_settings
from harness.db.enums import MessageRole, RunStatus
from harness.db.models import Tenant
from harness.mq import declare_topology, publish_run
from harness.runtime.repository import RunRepository
from harness.workers.main import process_message
from tests.fakes import ScriptedLLMGateway, get_message_with_wait, tool_call_response

RABBITMQ_URL = os.environ.get("HARNESS_TEST_RABBITMQ_URL", "amqp://harness:harness@localhost:5672/")


@pytest_asyncio.fixture
async def channel():
    connection = await aio_pika.connect_robust(RABBITMQ_URL)
    async with connection:
        ch = await connection.channel()
        await declare_topology(ch)
        main_queue = await ch.get_queue("agent.runs")
        await main_queue.purge()
        yield ch


@pytest.mark.asyncio
async def test_process_message_runs_to_completion_and_releases_lease(db_sessionmaker, channel):
    async with db_sessionmaker() as setup_session:
        tenant = Tenant(name=f"t-{uuid.uuid4()}")
        setup_session.add(tenant)
        await setup_session.flush()
        repo = RunRepository(setup_session)
        run = await repo.create_run(tenant_id=tenant.id, goal="check disk usage")
        await repo.add_message(run.id, MessageRole.user, "check disk usage")
        await setup_session.commit()
        run_id = run.id
        tenant_id = tenant.id

    await publish_run(channel, run_id, tenant_id)
    queue = await channel.get_queue("agent.runs")
    message = await get_message_with_wait(queue)

    llm = ScriptedLLMGateway([tool_call_response("finish", {"result": "disk usage is 42%"})])
    settings = get_settings()

    await process_message(message, db_sessionmaker, llm, settings)

    async with db_sessionmaker() as session:
        repo = RunRepository(session)
        final = await repo.get(run_id)
        assert final.status == RunStatus.completed
        assert final.final_result == {"result": "disk usage is 42%"}
        assert final.lease_owner is None


@pytest.mark.asyncio
async def test_process_message_skips_already_leased_run(db_sessionmaker, channel):
    async with db_sessionmaker() as setup_session:
        tenant = Tenant(name=f"t-{uuid.uuid4()}")
        setup_session.add(tenant)
        await setup_session.flush()
        repo = RunRepository(setup_session)
        run = await repo.create_run(tenant_id=tenant.id, goal="goal")
        await repo.add_message(run.id, MessageRole.user, "goal")
        acquired = await repo.try_acquire_lease(run, "some-other-worker", ttl_seconds=300)
        assert acquired
        await setup_session.commit()
        run_id = run.id
        tenant_id = tenant.id

    await publish_run(channel, run_id, tenant_id)
    queue = await channel.get_queue("agent.runs")
    message = await get_message_with_wait(queue)

    llm = ScriptedLLMGateway([])  # must not be called — run is already leased elsewhere
    settings = get_settings()

    await process_message(message, db_sessionmaker, llm, settings)

    async with db_sessionmaker() as session:
        repo = RunRepository(session)
        final = await repo.get(run_id)
        # Untouched — still leased by the other worker, never started.
        assert final.status == RunStatus.pending
        assert final.lease_owner == "some-other-worker"


@pytest.mark.asyncio
async def test_process_message_drops_message_for_missing_run(db_sessionmaker, channel):
    fake_run_id = uuid.uuid4()
    await publish_run(channel, fake_run_id, uuid.uuid4())
    queue = await channel.get_queue("agent.runs")
    message = await get_message_with_wait(queue)

    llm = ScriptedLLMGateway([])
    settings = get_settings()

    await process_message(message, db_sessionmaker, llm, settings)  # must not raise
