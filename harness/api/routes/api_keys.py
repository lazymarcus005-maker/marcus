import uuid

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from harness.api.deps import AuthPrincipal, require_admin
from harness.api.schemas import ApiKeyCreateRequest, ApiKeyCreateResponse, ApiKeyResponse
from harness.auth import create_api_key
from harness.db.models import ApiKey, User
from harness.db.session import get_session

router = APIRouter(prefix="/v1/api-keys", tags=["api-keys"])


@router.post("", response_model=ApiKeyCreateResponse, status_code=201)
async def create_key(
    body: ApiKeyCreateRequest,
    principal: AuthPrincipal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> ApiKeyCreateResponse:
    user = await session.get(User, body.user_id)
    if user is None or user.tenant_id != principal.tenant.id:
        raise HTTPException(status_code=404, detail="user not found")

    key, raw = await create_api_key(
        session,
        tenant_id=principal.tenant.id,
        user_id=user.id,
        name=body.name,
    )
    await session.commit()
    return ApiKeyCreateResponse(
        id=key.id,
        tenant_id=key.tenant_id,
        user_id=key.user_id,
        name=key.name,
        prefix=key.prefix,
        key=raw,
        enabled=key.enabled,
        created_at=key.created_at,
    )


@router.get("", response_model=list[ApiKeyResponse])
async def list_keys(
    principal: AuthPrincipal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> list[ApiKey]:
    result = await session.execute(
        sa.select(ApiKey).where(ApiKey.tenant_id == principal.tenant.id).order_by(ApiKey.created_at)
    )
    return list(result.scalars().all())


@router.delete("/{key_id}", response_model=ApiKeyResponse)
async def disable_key(
    key_id: uuid.UUID,
    principal: AuthPrincipal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> ApiKey:
    result = await session.execute(
        sa.select(ApiKey).where(ApiKey.id == key_id, ApiKey.tenant_id == principal.tenant.id)
    )
    key = result.scalar_one_or_none()
    if key is None:
        raise HTTPException(status_code=404, detail="api key not found")
    key.enabled = False
    await session.commit()
    return key
