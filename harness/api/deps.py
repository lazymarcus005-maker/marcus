import uuid

import sqlalchemy as sa
from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from harness.auth import AuthPrincipal, authenticate_api_key, tenant_has_api_keys
from harness.db.models import Tenant, User
from harness.db.session import get_session


def _extract_bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token


async def get_tenant_id(x_tenant_id: str = Header(...)) -> uuid.UUID:
    try:
        return uuid.UUID(x_tenant_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail="X-Tenant-Id header must be a valid UUID"
        ) from exc


async def require_principal(
    request: Request,
    x_tenant_id: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> AuthPrincipal:
    raw_key = x_api_key or _extract_bearer(authorization)
    if raw_key:
        return await authenticate_api_key(
            session, raw_key=raw_key, settings=request.app.state.settings
        )

    if not request.app.state.settings.legacy_auth_enabled:
        raise HTTPException(status_code=401, detail="missing API key")

    # Bootstrap/development compatibility: before a tenant has created its
    # first API key, existing X-Tenant-Id based tests and local clients can
    # still operate against that tenant. Once that tenant creates a key,
    # protected API requests against it require a key. This check is scoped
    # per-tenant so one tenant adopting API keys doesn't lock out every other
    # tenant that hasn't created one yet.
    if x_tenant_id is None:
        raise HTTPException(status_code=401, detail="missing API key")

    try:
        tenant_id = uuid.UUID(x_tenant_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail="X-Tenant-Id header must be a valid UUID"
        ) from exc

    if await tenant_has_api_keys(session, tenant_id):
        raise HTTPException(status_code=401, detail="missing API key")

    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="tenant not found")
    result = await session.execute(
        sa.select(User).where(User.tenant_id == tenant.id).order_by(User.created_at).limit(1)
    )
    return AuthPrincipal(tenant=tenant, user=result.scalar_one_or_none(), legacy=True)


async def require_tenant(principal: AuthPrincipal = Depends(require_principal)) -> Tenant:
    """Compatibility dependency for routes that only need tenant scope."""
    return principal.tenant


async def require_admin(principal: AuthPrincipal = Depends(require_principal)) -> AuthPrincipal:
    if not principal.is_admin:
        raise HTTPException(status_code=403, detail="admin role required")
    return principal


def member_can_access_run(principal: AuthPrincipal, run) -> bool:
    if principal.is_admin or principal.user is None:
        return True
    return run.created_by_user_id == principal.user.id
