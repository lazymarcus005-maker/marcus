import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from harness.db.enums import Channel, RiskTier, RunStatus, StepType, ToolExecutionStatus
from harness.db.models import AgentRun, AgentStep, Tenant, ToolExecution


async def _make_run(session, tenant_id) -> AgentRun:
    run = AgentRun(
        tenant_id=tenant_id,
        status=RunStatus.pending,
        goal="test goal",
        channel=Channel.api,
        max_steps=10,
        max_tool_calls=10,
        token_budget=1000,
        timeout_seconds=60,
    )
    session.add(run)
    await session.flush()
    return run


@pytest.mark.asyncio
async def test_agent_run_version_defaults_to_zero(db_session):
    tenant = Tenant(name=f"t-{uuid.uuid4()}")
    db_session.add(tenant)
    await db_session.flush()

    run = await _make_run(db_session, tenant.id)
    assert run.version == 0
    assert run.status == RunStatus.pending


@pytest.mark.asyncio
async def test_tool_execution_idempotency_key_is_unique(db_session):
    tenant = Tenant(name=f"t-{uuid.uuid4()}")
    db_session.add(tenant)
    await db_session.flush()
    run = await _make_run(db_session, tenant.id)

    key = f"{run.id}:0:0"
    db_session.add(
        ToolExecution(
            run_id=run.id,
            step_no=0,
            call_index=0,
            idempotency_key=key,
            tool_name="search",
            risk_tier=RiskTier.read_only,
            status=ToolExecutionStatus.started,
        )
    )
    await db_session.flush()

    db_session.add(
        ToolExecution(
            run_id=run.id,
            step_no=0,
            call_index=0,
            idempotency_key=key,
            tool_name="search",
            risk_tier=RiskTier.read_only,
            status=ToolExecutionStatus.started,
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.asyncio
async def test_agent_step_unique_per_run_and_step_no(db_session):
    tenant = Tenant(name=f"t-{uuid.uuid4()}")
    db_session.add(tenant)
    await db_session.flush()
    run = await _make_run(db_session, tenant.id)

    db_session.add(AgentStep(run_id=run.id, step_no=0, type=StepType.llm_call, payload={}))
    await db_session.flush()

    db_session.add(AgentStep(run_id=run.id, step_no=0, type=StepType.llm_call, payload={}))
    with pytest.raises(IntegrityError):
        await db_session.flush()
