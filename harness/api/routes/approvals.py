import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from harness.api.deps import require_tenant
from harness.api.schemas import ApprovalDecisionRequest, ApprovalRequestResponse
from harness.db.enums import ApprovalStatus, RunStatus
from harness.db.models import AgentRun, ApprovalRequest, Tenant
from harness.db.session import get_session
from harness.mq import publish_run_standalone
from harness.runtime.repository import RunRepository

router = APIRouter(tags=["approvals"])


@router.get("/v1/approvals", response_model=list[ApprovalRequestResponse])
async def list_approvals(
    tenant: Tenant = Depends(require_tenant),
    session: AsyncSession = Depends(get_session),
    status: ApprovalStatus | None = None,
) -> list[ApprovalRequest]:
    conditions = [ApprovalRequest.tenant_id == tenant.id]
    if status is not None:
        conditions.append(ApprovalRequest.status == status)
    result = await session.execute(
        sa.select(ApprovalRequest).where(*conditions).order_by(ApprovalRequest.requested_at.desc())
    )
    return list(result.scalars().all())


@router.get("/v1/runs/{run_id}/approvals", response_model=list[ApprovalRequestResponse])
async def list_run_approvals(
    run_id: uuid.UUID,
    tenant: Tenant = Depends(require_tenant),
    session: AsyncSession = Depends(get_session),
) -> list[ApprovalRequest]:
    result = await session.execute(
        sa.select(ApprovalRequest)
        .where(ApprovalRequest.run_id == run_id, ApprovalRequest.tenant_id == tenant.id)
        .order_by(ApprovalRequest.step_no, ApprovalRequest.call_index)
    )
    return list(result.scalars().all())


@router.post("/v1/approvals/{approval_id}/decide", response_model=ApprovalRequestResponse)
async def decide_approval(
    approval_id: uuid.UUID,
    body: ApprovalDecisionRequest,
    tenant: Tenant = Depends(require_tenant),
    session: AsyncSession = Depends(get_session),
) -> ApprovalRequest:
    if body.decision not in (ApprovalStatus.approved, ApprovalStatus.rejected):
        raise HTTPException(status_code=400, detail="decision must be 'approved' or 'rejected'")

    result = await session.execute(
        sa.select(ApprovalRequest).where(
            ApprovalRequest.id == approval_id, ApprovalRequest.tenant_id == tenant.id
        )
    )
    approval = result.scalar_one_or_none()
    if approval is None:
        raise HTTPException(status_code=404, detail="approval request not found")
    if approval.status != ApprovalStatus.pending:
        raise HTTPException(
            status_code=409, detail=f"approval request already {approval.status}"
        )

    approval.status = body.decision
    approval.reason = body.reason
    approval.decided_by_user_id = body.decided_by_user_id
    approval.decided_at = datetime.now(UTC)
    await session.flush()

    repo = RunRepository(session)
    run = await session.get(AgentRun, approval.run_id)
    if run is not None and run.status == RunStatus.waiting_approval:
        # Resume regardless of the decision — the engine needs a step to
        # record either the tool's success or the rejection observation.
        run = await repo.checkpoint(run, status=RunStatus.running)
        await session.commit()
        await publish_run_standalone(run.id, run.tenant_id)
    else:
        await session.commit()

    return approval
