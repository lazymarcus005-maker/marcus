import asyncio

from harness.auth import create_api_key
from harness.db.enums import UserRole
from harness.db.models import Tenant, User
from harness.db.session import get_sessionmaker


async def seed_default_tenant() -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant = Tenant(name="default")
        session.add(tenant)
        await session.flush()

        admin = User(tenant_id=tenant.id, display_name="admin", role=UserRole.admin)
        session.add(admin)
        await session.flush()
        _key, raw_key = await create_api_key(
            session, tenant_id=tenant.id, user_id=admin.id, name="default-admin"
        )
        await session.commit()
        print(f"seeded tenant 'default' ({tenant.id})")
        print(f"default admin API key: {raw_key}")


if __name__ == "__main__":
    asyncio.run(seed_default_tenant())
