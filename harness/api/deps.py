import uuid

from fastapi import Depends, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from harness.db.models import Tenant
from harness.db.session import get_session


async def get_tenant_id(x_tenant_id: str = Header(...)) -> uuid.UUID:
    try:
        return uuid.UUID(x_tenant_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail="X-Tenant-Id header must be a valid UUID"
        ) from exc


async def require_tenant(
    tenant_id: uuid.UUID = Depends(get_tenant_id),
    session: AsyncSession = Depends(get_session),
) -> Tenant:
    """Resolves the calling tenant from X-Tenant-Id. Not authentication — that's

    issue #23. This only prevents operating against a nonexistent tenant and
    gives every route a consistent way to scope queries.
    """
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="tenant not found")
    return tenant
