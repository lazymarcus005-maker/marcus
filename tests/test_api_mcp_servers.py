import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from harness.api.app import create_app
from harness.db.models import Tenant
from harness.db.session import get_session


@pytest.fixture
def app(db_sessionmaker):
    application = create_app()

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


@pytest_asyncio.fixture
async def tenant_id(db_sessionmaker):
    async with db_sessionmaker() as session:
        tenant = Tenant(name=f"t-{uuid.uuid4()}")
        session.add(tenant)
        await session.commit()
        return tenant.id


@pytest.mark.asyncio
async def test_register_server_does_not_return_auth_header_value(client, tenant_id):
    headers = {"X-Tenant-Id": str(tenant_id)}
    resp = await client.post(
        "/v1/mcp-servers",
        json={
            "name": "gitlab",
            "base_url": "https://mcp.internal/gitlab",
            "auth_header_name": "Authorization",
            "auth_header_value": "Bearer secret-token",
            "default_risk_tier": "low_risk_write",
        },
        headers=headers,
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "gitlab"
    assert body["auth_header_name"] == "Authorization"
    assert "auth_header_value" not in body
    assert "auth_header_value_encrypted" not in body
    assert body["health_status"] == "unknown"


@pytest.mark.asyncio
async def test_list_servers_scoped_to_tenant(client, tenant_id):
    headers = {"X-Tenant-Id": str(tenant_id)}
    await client.post(
        "/v1/mcp-servers",
        json={"name": "elastic", "base_url": "https://mcp.internal/elastic"},
        headers=headers,
    )

    other_headers = {"X-Tenant-Id": str(uuid.uuid4())}
    resp = await client.get("/v1/mcp-servers", headers=other_headers)
    assert resp.status_code == 404  # unknown tenant

    resp = await client.get("/v1/mcp-servers", headers=headers)
    assert resp.status_code == 200
    assert [s["name"] for s in resp.json()] == ["elastic"]


@pytest.mark.asyncio
async def test_update_server_enabled_flag(client, tenant_id):
    headers = {"X-Tenant-Id": str(tenant_id)}
    create_resp = await client.post(
        "/v1/mcp-servers",
        json={"name": "elastic", "base_url": "https://mcp.internal/elastic"},
        headers=headers,
    )
    server_id = create_resp.json()["id"]

    resp = await client.patch(
        f"/v1/mcp-servers/{server_id}", json={"enabled": False}, headers=headers
    )
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False


@pytest.mark.asyncio
async def test_get_tools_for_unknown_server_returns_404(client, tenant_id):
    headers = {"X-Tenant-Id": str(tenant_id)}
    resp = await client.get(f"/v1/mcp-servers/{uuid.uuid4()}/tools", headers=headers)
    assert resp.status_code == 404
