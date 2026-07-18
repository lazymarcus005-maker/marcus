import uuid
from datetime import UTC, datetime, time

import sqlalchemy as sa
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from harness.config import Settings
from harness.db.enums import TERMINAL_RUN_STATUSES
from harness.db.models import AgentRun, TenantQuota, UsageRecord


async def get_quota(session: AsyncSession, tenant_id: uuid.UUID, settings: Settings) -> TenantQuota:
    quota = await session.get(TenantQuota, tenant_id)
    if quota is not None:
        return quota
    quota = TenantQuota(
        tenant_id=tenant_id,
        daily_token_quota=settings.tenant_daily_token_quota_default,
        max_active_runs=settings.tenant_max_active_runs_default,
    )
    session.add(quota)
    await session.flush()
    return quota


async def enforce_tenant_run_quota(
    session: AsyncSession, *, tenant_id: uuid.UUID, settings: Settings
) -> None:
    quota = await get_quota(session, tenant_id, settings)
    active_result = await session.execute(
        sa.select(sa.func.count())
        .select_from(AgentRun)
        .where(AgentRun.tenant_id == tenant_id, AgentRun.status.not_in(TERMINAL_RUN_STATUSES))
    )
    if (active_result.scalar_one() or 0) >= quota.max_active_runs:
        raise HTTPException(status_code=429, detail="tenant active run quota exceeded")

    start = datetime.combine(datetime.now(UTC).date(), time.min, tzinfo=UTC)
    tokens_result = await session.execute(
        sa.select(sa.func.coalesce(sa.func.sum(UsageRecord.total_tokens), 0)).where(
            UsageRecord.tenant_id == tenant_id, UsageRecord.created_at >= start
        )
    )
    if (tokens_result.scalar_one() or 0) >= quota.daily_token_quota:
        raise HTTPException(status_code=429, detail="tenant daily token quota exceeded")
