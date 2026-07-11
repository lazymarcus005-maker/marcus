import uuid

import pytest
import sqlalchemy as sa

from harness.db.enums import MessageRole, RiskTier, ToolExecutionStatus
from harness.db.models import Tenant, ToolExecution
from harness.llm.types import ToolCall
from harness.runtime.repository import RunRepository
from harness.runtime.tool_executor import ToolExecutor
from harness.runtime.tools import Tool


async def _make_run(session):
    tenant = Tenant(name=f"t-{uuid.uuid4()}")
    session.add(tenant)
    await session.flush()
    repo = RunRepository(session)
    run = await repo.create_run(tenant_id=tenant.id, goal="goal")
    await repo.add_message(run.id, MessageRole.user, "goal")
    await session.commit()
    return run


async def _get_execution(session, idempotency_key: str) -> ToolExecution:
    result = await session.execute(
        sa.select(ToolExecution).where(ToolExecution.idempotency_key == idempotency_key)
    )
    return result.scalar_one()


@pytest.mark.asyncio
async def test_execute_writes_started_row_before_calling_handler(db_session):
    run = await _make_run(db_session)
    handler_invoked = {"count": 0}

    async def handler(arguments: dict) -> dict:
        handler_invoked["count"] += 1
        return {"ok": True}

    tool = Tool(name="do_thing", description="d", parameters={}, handler=handler)
    executor = ToolExecutor(db_session)
    call = ToolCall(id="call_1", name="do_thing", arguments={"x": 1})

    outcome = await executor.execute(run, 0, 0, call, tool=tool)

    assert outcome.observation == {"ok": True}
    assert handler_invoked["count"] == 1

    execution = await _get_execution(db_session, f"{run.id}:0:0")
    assert execution.status == ToolExecutionStatus.succeeded


@pytest.mark.asyncio
async def test_read_only_crash_recovery_retries_automatically(db_sessionmaker):
    async with db_sessionmaker() as setup_session:
        run = await _make_run(setup_session)
        run_id = run.id

    # Simulate a crash: a "started" row exists (write-ahead succeeded) but the
    # handler never got to run and finalize the row.
    async with db_sessionmaker() as crash_session:
        run = await RunRepository(crash_session).get(run_id)
        crash_session.add(
            ToolExecution(
                run_id=run.id,
                step_no=0,
                call_index=0,
                idempotency_key=f"{run.id}:0:0",
                tool_name="search",
                risk_tier=RiskTier.read_only,
                idempotent=True,
                args={"query": "x"},
                status=ToolExecutionStatus.started,
            )
        )
        await crash_session.commit()

    async with db_sessionmaker() as resume_session:
        run = await RunRepository(resume_session).get(run_id)
        handler_invoked = {"count": 0}

        async def handler(arguments: dict) -> dict:
            handler_invoked["count"] += 1
            return {"hits": []}

        tool = Tool(
            name="search",
            description="d",
            parameters={},
            handler=handler,
            risk_tier=RiskTier.read_only,
        )
        executor = ToolExecutor(resume_session)
        call = ToolCall(id="call_1", name="search", arguments={"query": "x"})

        outcome = await executor.execute(run, 0, 0, call, tool=tool)

        assert handler_invoked["count"] == 1
        assert outcome.observation == {"hits": []}
        assert not outcome.fatal


@pytest.mark.asyncio
async def test_destructive_crash_recovery_marks_unknown_and_is_fatal(db_sessionmaker):
    async with db_sessionmaker() as setup_session:
        run = await _make_run(setup_session)
        run_id = run.id

    async with db_sessionmaker() as crash_session:
        run = await RunRepository(crash_session).get(run_id)
        crash_session.add(
            ToolExecution(
                run_id=run.id,
                step_no=0,
                call_index=0,
                idempotency_key=f"{run.id}:0:0",
                tool_name="delete_resource",
                risk_tier=RiskTier.destructive,
                idempotent=False,
                args={"id": "abc"},
                status=ToolExecutionStatus.started,
            )
        )
        await crash_session.commit()

    async with db_sessionmaker() as resume_session:
        run = await RunRepository(resume_session).get(run_id)
        handler_invoked = {"count": 0}

        async def handler(arguments: dict) -> dict:
            handler_invoked["count"] += 1
            return {"deleted": True}

        tool = Tool(
            name="delete_resource",
            description="d",
            parameters={},
            handler=handler,
            risk_tier=RiskTier.destructive,
        )
        executor = ToolExecutor(resume_session)
        call = ToolCall(id="call_1", name="delete_resource", arguments={"id": "abc"})

        outcome = await executor.execute(run, 0, 0, call, tool=tool)

        # Must NOT auto-retry a destructive tool — the handler is never called again.
        assert handler_invoked["count"] == 0
        assert outcome.fatal
        assert "unknown" in outcome.observation["error"]

        execution = await _get_execution(resume_session, f"{run.id}:0:0")
        assert execution.status == ToolExecutionStatus.unknown


@pytest.mark.asyncio
async def test_successful_execution_is_not_repeated_on_replay(db_session):
    run = await _make_run(db_session)
    handler_invoked = {"count": 0}

    async def handler(arguments: dict) -> dict:
        handler_invoked["count"] += 1
        return {"call": handler_invoked["count"]}

    tool = Tool(name="do_thing", description="d", parameters={}, handler=handler)
    executor = ToolExecutor(db_session)
    call = ToolCall(id="call_1", name="do_thing", arguments={"x": 1})

    first = await executor.execute(run, 0, 0, call, tool=tool)
    second = await executor.execute(run, 0, 0, call, tool=tool)

    assert handler_invoked["count"] == 1
    assert first.observation == second.observation == {"call": 1}


@pytest.mark.asyncio
async def test_unknown_tool_records_failure_without_calling_any_handler(db_session):
    run = await _make_run(db_session)
    executor = ToolExecutor(db_session)
    call = ToolCall(id="call_1", name="nonexistent", arguments={})

    outcome = await executor.execute(run, 0, 0, call, tool=None)

    assert "unknown tool" in outcome.observation["error"]
    execution = await _get_execution(db_session, f"{run.id}:0:0")
    assert execution.status == ToolExecutionStatus.failed
