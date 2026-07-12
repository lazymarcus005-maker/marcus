import uuid

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from harness.api.deps import AuthPrincipal, require_admin
from harness.api.schemas import (
    ScheduledJobCreateRequest,
    ScheduledJobResponse,
    ScheduledJobUpdateRequest,
)
from harness.db.models import ScheduledJob
from harness.db.session import get_session
from harness.scheduler import refresh_job_next_run, validate_cron

router = APIRouter(prefix="/v1/scheduled-jobs", tags=["scheduled-jobs"])


@router.post("", response_model=ScheduledJobResponse, status_code=201)
async def create_scheduled_job(
    body: ScheduledJobCreateRequest,
    principal: AuthPrincipal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> ScheduledJob:
    try:
        validate_cron(body.cron_expression)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    job = ScheduledJob(
        tenant_id=principal.tenant.id,
        name=body.name,
        cron_expression=body.cron_expression,
        goal=body.goal,
        channel=body.channel,
        channel_metadata=body.channel_metadata,
        enabled=body.enabled,
    )
    await refresh_job_next_run(job)
    session.add(job)
    await session.commit()
    return job


@router.get("", response_model=list[ScheduledJobResponse])
async def list_scheduled_jobs(
    principal: AuthPrincipal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> list[ScheduledJob]:
    result = await session.execute(
        sa.select(ScheduledJob)
        .where(ScheduledJob.tenant_id == principal.tenant.id)
        .order_by(ScheduledJob.name)
    )
    return list(result.scalars().all())


@router.patch("/{job_id}", response_model=ScheduledJobResponse)
async def update_scheduled_job(
    job_id: uuid.UUID,
    body: ScheduledJobUpdateRequest,
    principal: AuthPrincipal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> ScheduledJob:
    result = await session.execute(
        sa.select(ScheduledJob).where(
            ScheduledJob.id == job_id, ScheduledJob.tenant_id == principal.tenant.id
        )
    )
    job = result.scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=404, detail="scheduled job not found")
    if body.cron_expression is not None:
        try:
            validate_cron(body.cron_expression)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        job.cron_expression = body.cron_expression
    if body.goal is not None:
        job.goal = body.goal
    if body.channel is not None:
        job.channel = body.channel
    if body.channel_metadata is not None:
        job.channel_metadata = body.channel_metadata
    if body.enabled is not None:
        job.enabled = body.enabled
    await refresh_job_next_run(job)
    await session.commit()
    return job
