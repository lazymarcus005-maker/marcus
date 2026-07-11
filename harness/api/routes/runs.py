import uuid

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from harness.api.deps import require_tenant
from harness.api.schemas import (
    MessageCreateRequest,
    RunCreateRequest,
    RunListResponse,
    RunResponse,
    RunStepsResponse,
    StepResponse,
    ToolExecutionResponse,
)
from harness.db.enums import TERMINAL_RUN_STATUSES, WAITING_RUN_STATUSES, MessageRole, RunStatus
from harness.db.models import AgentRun, AgentStep, Tenant, ToolExecution
from harness.db.session import get_session
from harness.mq import publish_run_standalone
from harness.runtime.repository import RunRepository

router = APIRouter(prefix="/v1/runs", tags=["runs"])


async def _get_owned_run(session: AsyncSession, tenant: Tenant, run_id: uuid.UUID) -> AgentRun:
    repo = RunRepository(session)
    run = await repo.get(run_id)
    if run is None or run.tenant_id != tenant.id:
        raise HTTPException(status_code=404, detail="run not found")
    return run


@router.post("", response_model=RunResponse, status_code=201)
async def create_run(
    body: RunCreateRequest,
    tenant: Tenant = Depends(require_tenant),
    session: AsyncSession = Depends(get_session),
) -> AgentRun:
    repo = RunRepository(session)
    run = await repo.create_run(
        tenant_id=tenant.id,
        goal=body.goal,
        channel=body.channel,
        channel_metadata=body.channel_metadata,
        max_steps=body.max_steps,
        max_tool_calls=body.max_tool_calls,
        token_budget=body.token_budget,
        timeout_seconds=body.timeout_seconds,
    )
    await repo.add_message(run.id, MessageRole.user, body.goal)
    await session.commit()

    await publish_run_standalone(run.id, tenant.id)
    return run


@router.get("", response_model=RunListResponse)
async def list_runs(
    tenant: Tenant = Depends(require_tenant),
    session: AsyncSession = Depends(get_session),
    status: RunStatus | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> RunListResponse:
    conditions = [AgentRun.tenant_id == tenant.id]
    if status is not None:
        conditions.append(AgentRun.status == status)

    count_result = await session.execute(
        sa.select(sa.func.count()).select_from(AgentRun).where(*conditions)
    )
    total = count_result.scalar_one()

    result = await session.execute(
        sa.select(AgentRun)
        .where(*conditions)
        .order_by(AgentRun.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    items = list(result.scalars().all())
    return RunListResponse(
        items=[RunResponse.model_validate(r) for r in items],
        limit=limit,
        offset=offset,
        total=total,
    )


@router.get("/{run_id}", response_model=RunResponse)
async def get_run(
    run_id: uuid.UUID,
    tenant: Tenant = Depends(require_tenant),
    session: AsyncSession = Depends(get_session),
) -> AgentRun:
    return await _get_owned_run(session, tenant, run_id)


@router.get("/{run_id}/steps", response_model=RunStepsResponse)
async def get_run_steps(
    run_id: uuid.UUID,
    tenant: Tenant = Depends(require_tenant),
    session: AsyncSession = Depends(get_session),
) -> RunStepsResponse:
    await _get_owned_run(session, tenant, run_id)

    steps_result = await session.execute(
        sa.select(AgentStep).where(AgentStep.run_id == run_id).order_by(AgentStep.step_no)
    )
    executions_result = await session.execute(
        sa.select(ToolExecution)
        .where(ToolExecution.run_id == run_id)
        .order_by(ToolExecution.step_no, ToolExecution.call_index)
    )
    return RunStepsResponse(
        steps=[StepResponse.model_validate(s) for s in steps_result.scalars().all()],
        tool_executions=[
            ToolExecutionResponse.model_validate(e) for e in executions_result.scalars().all()
        ],
    )


@router.post("/{run_id}/messages", response_model=RunResponse)
async def post_message(
    run_id: uuid.UUID,
    body: MessageCreateRequest,
    tenant: Tenant = Depends(require_tenant),
    session: AsyncSession = Depends(get_session),
) -> AgentRun:
    repo = RunRepository(session)
    run = await _get_owned_run(session, tenant, run_id)

    if run.status not in WAITING_RUN_STATUSES:
        raise HTTPException(
            status_code=409, detail=f"run is not waiting for input (status={run.status})"
        )

    await repo.add_message(run.id, MessageRole.user, body.content)
    run = await repo.checkpoint(run, status=RunStatus.running)
    await session.commit()

    await publish_run_standalone(run.id, tenant.id)
    return run


@router.post("/{run_id}/cancel", response_model=RunResponse)
async def cancel_run(
    run_id: uuid.UUID,
    tenant: Tenant = Depends(require_tenant),
    session: AsyncSession = Depends(get_session),
) -> AgentRun:
    repo = RunRepository(session)
    run = await _get_owned_run(session, tenant, run_id)

    if run.status in TERMINAL_RUN_STATUSES:
        raise HTTPException(status_code=409, detail=f"run is already {run.status}")

    if run.status in WAITING_RUN_STATUSES:
        # No active step loop will ever observe cancel_requested for a parked
        # run, so cancel it outright instead of flagging it for a step that
        # isn't coming.
        run = await repo.checkpoint(run, status=RunStatus.cancelled, cancel_requested=True)
    else:
        # Pending or Running: the engine's guardrail checks this flag before
        # its next step and transitions to Cancelled itself.
        run = await repo.checkpoint(run, cancel_requested=True)

    await session.commit()
    return run
