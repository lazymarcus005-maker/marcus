import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from harness.api.app import create_app
from harness.auth import create_api_key
from harness.db.enums import UserRole
from harness.db.models import Tenant, User
from harness.db.session import get_session


@pytest.fixture
def app(db_sessionmaker):
    application = create_app()
    application.state.settings.api_key_rate_limit_per_minute = 0
    application.state.settings.web_base_url = "http://web"

    async def override_get_session():
        async with db_sessionmaker() as session:
            yield session

    application.dependency_overrides[get_session] = override_get_session
    return application


@pytest_asyncio.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def _admin_key(session):
    tenant = Tenant(name=f"t-{uuid.uuid4()}")
    session.add(tenant)
    await session.flush()
    user = User(tenant_id=tenant.id, display_name="admin", role=UserRole.admin)
    session.add(user)
    await session.flush()
    _key, raw = await create_api_key(session, tenant_id=tenant.id, user_id=user.id, name="admin")
    await session.commit()
    return tenant, raw


@pytest.mark.asyncio
async def test_get_slack_settings_returns_null_when_unconfigured(client, db_sessionmaker):
    async with db_sessionmaker() as session:
        _tenant, raw = await _admin_key(session)

    resp = await client.get("/v1/slack-settings", headers={"X-API-Key": raw})

    assert resp.status_code == 200
    assert resp.json() is None


@pytest.mark.asyncio
async def test_put_slack_settings_creates_and_hides_secrets(client, db_sessionmaker):
    async with db_sessionmaker() as session:
        _tenant, raw = await _admin_key(session)

    resp = await client.put(
        "/v1/slack-settings",
        json={"bot_token": "xoxb-secret", "signing_secret": "shh-secret"},
        headers={"X-API-Key": raw},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["has_bot_token"] is True
    assert body["has_signing_secret"] is True
    assert body["webhook_url"] == "http://web/v1/slack/events"
    assert "xoxb-secret" not in resp.text
    assert "shh-secret" not in resp.text


@pytest.mark.asyncio
async def test_put_slack_settings_without_both_fields_requires_them_on_create(
    client, db_sessionmaker
):
    async with db_sessionmaker() as session:
        _tenant, raw = await _admin_key(session)

    resp = await client.put(
        "/v1/slack-settings",
        json={"bot_token": "xoxb-only"},
        headers={"X-API-Key": raw},
    )

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_delete_slack_settings_reverts_to_unconfigured(client, db_sessionmaker):
    async with db_sessionmaker() as session:
        _tenant, raw = await _admin_key(session)

    await client.put(
        "/v1/slack-settings",
        json={"bot_token": "xoxb-secret", "signing_secret": "shh-secret"},
        headers={"X-API-Key": raw},
    )

    delete_resp = await client.delete("/v1/slack-settings", headers={"X-API-Key": raw})
    assert delete_resp.status_code == 204

    get_resp = await client.get("/v1/slack-settings", headers={"X-API-Key": raw})
    assert get_resp.json() is None


@pytest.mark.asyncio
async def test_slack_settings_require_admin(client, db_sessionmaker):
    async with db_sessionmaker() as session:
        tenant = Tenant(name=f"t-{uuid.uuid4()}")
        session.add(tenant)
        await session.flush()
        member = User(tenant_id=tenant.id, display_name="member", role=UserRole.member)
        session.add(member)
        await session.flush()
        _key, raw = await create_api_key(
            session, tenant_id=tenant.id, user_id=member.id, name="member"
        )
        await session.commit()

    resp = await client.get("/v1/slack-settings", headers={"X-API-Key": raw})

    assert resp.status_code == 403
