import uuid

import pytest
from mcp.types import Tool as McpToolSpec

from harness.db.enums import McpHealthStatus, RiskTier
from harness.db.models import Tenant
from harness.mcp import crypto
from harness.mcp.client import McpError
from harness.mcp.registry import McpRegistry
from tests.fakes import FakeMcpClient


async def _make_tenant(session):
    tenant = Tenant(name=f"t-{uuid.uuid4()}")
    session.add(tenant)
    await session.flush()
    return tenant


def test_crypto_roundtrip():
    encrypted = crypto.encrypt("super-secret-token")
    assert encrypted != b"super-secret-token"
    assert crypto.decrypt(encrypted) == "super-secret-token"


@pytest.mark.asyncio
async def test_register_encrypts_auth_header_value(db_session):
    tenant = await _make_tenant(db_session)
    registry = McpRegistry(db_session)

    server = await registry.register(
        tenant_id=tenant.id,
        name="elastic",
        base_url="https://mcp.internal/elastic",
        auth_header_name="Authorization",
        auth_header_value="Bearer abc123",
        default_risk_tier=RiskTier.read_only,
    )
    await db_session.commit()

    assert server.auth_header_value_encrypted != b"Bearer abc123"
    assert crypto.decrypt(server.auth_header_value_encrypted) == "Bearer abc123"


@pytest.mark.asyncio
async def test_refresh_tools_upserts_and_seeds_risk_tier_from_server_default(db_session):
    tenant = await _make_tenant(db_session)
    registry = McpRegistry(
        db_session,
        client=FakeMcpClient(
            tools_by_server={
                "gitlab": [
                    McpToolSpec(
                        name="create_issue",
                        description="Create a GitLab issue.",
                        inputSchema={"type": "object", "properties": {"title": {"type": "string"}}},
                    )
                ]
            }
        ),
    )
    server = await registry.register(
        tenant_id=tenant.id,
        name="gitlab",
        base_url="https://mcp.internal/gitlab",
        default_risk_tier=RiskTier.low_risk_write,
    )
    await db_session.commit()

    count = await registry.refresh_tools(server)
    await db_session.commit()

    assert count == 1
    assert server.health_status == McpHealthStatus.healthy

    tools = await registry.list_tools(tenant.id)
    assert len(tools) == 1
    _srv, tool = tools[0]
    assert tool.name == "create_issue"
    assert tool.risk_tier == RiskTier.low_risk_write  # seeded from server default
    assert tool.parameters == {"type": "object", "properties": {"title": {"type": "string"}}}


@pytest.mark.asyncio
async def test_refresh_tools_disables_tools_no_longer_reported(db_session):
    tenant = await _make_tenant(db_session)
    client = FakeMcpClient(
        tools_by_server={
            "gitlab": [
                McpToolSpec(name="create_issue", description="d", inputSchema={}),
                McpToolSpec(name="close_issue", description="d", inputSchema={}),
            ]
        }
    )
    registry = McpRegistry(db_session, client=client)
    server = await registry.register(
        tenant_id=tenant.id, name="gitlab", base_url="https://mcp.internal/gitlab"
    )
    await db_session.commit()
    await registry.refresh_tools(server)
    await db_session.commit()

    # Server stops reporting close_issue.
    client.tools_by_server["gitlab"] = [
        McpToolSpec(name="create_issue", description="d", inputSchema={})
    ]
    await registry.refresh_tools(server)
    await db_session.commit()

    tools = await registry.list_tools(tenant.id)
    assert [t.name for _s, t in tools] == ["create_issue"]

    close_issue = await registry.get_tool_by_name(tenant.id, "close_issue")
    assert close_issue is not None
    assert close_issue.enabled is False


@pytest.mark.asyncio
async def test_refresh_tools_marks_unhealthy_on_transport_failure(db_session):
    tenant = await _make_tenant(db_session)
    client = FakeMcpClient(call_results={})

    async def _failing_list_tools(server):
        raise McpError("connection refused")

    client.list_tools = _failing_list_tools  # type: ignore[method-assign]
    registry = McpRegistry(db_session, client=client)
    server = await registry.register(
        tenant_id=tenant.id, name="flaky", base_url="https://mcp.internal/flaky"
    )
    await db_session.commit()

    with pytest.raises(McpError):
        await registry.refresh_tools(server)
    await db_session.commit()

    assert server.health_status == McpHealthStatus.unhealthy
    assert "connection refused" in server.last_error
