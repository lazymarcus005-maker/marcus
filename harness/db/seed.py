import asyncio

from harness.db.enums import UserRole
from harness.db.models import Tenant, User
from harness.db.session import get_sessionmaker


async def seed_default_tenant() -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant = Tenant(name="default")
        session.add(tenant)
        await session.flush()

        session.add(User(tenant_id=tenant.id, display_name="admin", role=UserRole.admin))
        await session.commit()
        print(f"seeded tenant 'default' ({tenant.id})")


if __name__ == "__main__":
    asyncio.run(seed_default_tenant())
