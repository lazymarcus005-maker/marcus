import sqlalchemy as sa
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from harness.api.deps import AuthPrincipal, require_admin
from harness.db.enums import TERMINAL_RUN_STATUSES, ApprovalStatus
from harness.db.models import AgentRun, ApprovalRequest, TenantQuota
from harness.db.session import get_session

router = APIRouter(prefix="/v1/ops", tags=["ops"])


@router.get("/summary")
async def ops_summary(
    principal: AuthPrincipal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    active_runs = (
        await session.execute(
            sa.select(sa.func.count()).select_from(AgentRun).where(
                AgentRun.tenant_id == principal.tenant.id,
                AgentRun.status.not_in(TERMINAL_RUN_STATUSES),
            )
        )
    ).scalar_one()
    pending_approvals = (
        await session.execute(
            sa.select(sa.func.count()).select_from(ApprovalRequest).where(
                ApprovalRequest.tenant_id == principal.tenant.id,
                ApprovalRequest.status == ApprovalStatus.pending,
            )
        )
    ).scalar_one()
    current_quota = await session.get(TenantQuota, principal.tenant.id)
    return {
        "active_runs": active_runs,
        "pending_approvals": pending_approvals,
        "quota": {
            "daily_token_quota": current_quota.daily_token_quota if current_quota else None,
            "max_active_runs": current_quota.max_active_runs if current_quota else None,
        },
    }
