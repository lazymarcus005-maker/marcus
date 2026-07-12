import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import redis.asyncio as redis
import sqlalchemy as sa
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from harness.config import Settings
from harness.db.enums import UserRole
from harness.db.models import ApiKey, Tenant, User

KEY_PREFIX = "hrn_"


@dataclass
class AuthPrincipal:
    tenant: Tenant
    user: User | None
    api_key: ApiKey | None = None
    legacy: bool = False

    @property
    def role(self) -> UserRole:
        if self.user is None:
            return UserRole.admin
        return UserRole(self.user.role)

    @property
    def is_admin(self) -> bool:
        return self.role == UserRole.admin


def generate_api_key() -> str:
    return f"{KEY_PREFIX}{secrets.token_urlsafe(32)}"


def hash_api_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode()).hexdigest()


def api_key_prefix(raw_key: str) -> str:
    return raw_key[:12]


async def create_api_key(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    name: str,
    raw_key: str | None = None,
) -> tuple[ApiKey, str]:
    raw = raw_key or generate_api_key()
    key = ApiKey(
        tenant_id=tenant_id,
        user_id=user_id,
        name=name,
        prefix=api_key_prefix(raw),
        key_hash=hash_api_key(raw),
    )
    session.add(key)
    await session.flush()
    return key, raw


async def tenant_has_api_keys(session: AsyncSession, tenant_id: uuid.UUID) -> bool:
    result = await session.execute(
        sa.select(sa.func.count()).select_from(ApiKey).where(ApiKey.tenant_id == tenant_id)
    )
    return (result.scalar_one() or 0) > 0


async def authenticate_api_key(
    session: AsyncSession, *, raw_key: str, settings: Settings
) -> AuthPrincipal:
    result = await session.execute(
        sa.select(ApiKey, Tenant, User)
        .join(Tenant, ApiKey.tenant_id == Tenant.id)
        .join(User, ApiKey.user_id == User.id)
        .where(ApiKey.key_hash == hash_api_key(raw_key), ApiKey.enabled.is_(True))
    )
    row = result.one_or_none()
    if row is None:
        raise HTTPException(status_code=401, detail="invalid API key")

    key, tenant, user = row.ApiKey, row.Tenant, row.User
    await enforce_rate_limit(settings=settings, key_id=key.id)
    key.last_used_at = datetime.now(UTC)
    # Commit (not just flush) since GET routes never call session.commit()
    # themselves; without an explicit commit here this update is silently
    # discarded when the request's session closes.
    await session.commit()
    return AuthPrincipal(tenant=tenant, user=user, api_key=key)


async def enforce_rate_limit(settings: Settings, *, key_id: uuid.UUID) -> None:
    limit = settings.api_key_rate_limit_per_minute
    if limit <= 0:
        return
    client = redis.from_url(settings.redis_url, decode_responses=True)
    try:
        bucket = f"rate-limit:api-key:{key_id}:{int(datetime.now(UTC).timestamp() // 60)}"
        count = await client.incr(bucket)
        if count == 1:
            await client.expire(bucket, 70)
        if count > limit:
            raise HTTPException(status_code=429, detail="API key rate limit exceeded")
    finally:
        await client.aclose()
