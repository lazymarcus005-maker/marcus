import uuid

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from harness.api.deps import require_tenant
from harness.api.schemas import (
    McpServerCreateRequest,
    McpServerRefreshResponse,
    McpServerResponse,
    McpServerUpdateRequest,
    McpToolResponse,
    McpToolUpdateRequest,
)
from harness.db.models import McpServer, McpTool, Tenant
from harness.db.session import get_session
from harness.mcp.client import McpError
from harness.mcp.crypto import encrypt
from harness.mcp.registry import McpRegistry

router = APIRouter(prefix="/v1/mcp-servers", tags=["mcp-servers"])


async def _get_owned_server(session: AsyncSession, tenant: Tenant, server_id: uuid.UUID) -> McpServer:
    server = await McpRegistry(session).get_server(tenant.id, server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="mcp server not found")
    return server


@router.post("", response_model=McpServerResponse, status_code=201)
async def register_server(
    body: McpServerCreateRequest,
    tenant: Tenant = Depends(require_tenant),
    session: AsyncSession = Depends(get_session),
) -> McpServer:
    server = await McpRegistry(session).register(
        tenant_id=tenant.id,
        name=body.name,
        base_url=body.base_url,
        auth_header_name=body.auth_header_name,
        auth_header_value=body.auth_header_value,
        default_risk_tier=body.default_risk_tier,
    )
    await session.commit()
    return server


@router.get("", response_model=list[McpServerResponse])
async def list_servers(
    tenant: Tenant = Depends(require_tenant),
    session: AsyncSession = Depends(get_session),
) -> list[McpServer]:
    return await McpRegistry(session).list_servers(tenant.id)


@router.patch("/{server_id}", response_model=McpServerResponse)
async def update_server(
    server_id: uuid.UUID,
    body: McpServerUpdateRequest,
    tenant: Tenant = Depends(require_tenant),
    session: AsyncSession = Depends(get_session),
) -> McpServer:
    server = await _get_owned_server(session, tenant, server_id)

    if body.base_url is not None:
        server.base_url = body.base_url
    if body.auth_header_name is not None:
        server.auth_header_name = body.auth_header_name
    if body.auth_header_value is not None:
        server.auth_header_value_encrypted = encrypt(body.auth_header_value)
    if body.default_risk_tier is not None:
        server.default_risk_tier = body.default_risk_tier
    if body.enabled is not None:
        server.enabled = body.enabled

    await session.commit()
    await session.refresh(server)  # updated_at is server-computed (onupdate=func.now())
    return server


@router.post("/{server_id}/refresh", response_model=McpServerRefreshResponse)
async def refresh_server_tools(
    server_id: uuid.UUID,
    tenant: Tenant = Depends(require_tenant),
    session: AsyncSession = Depends(get_session),
) -> McpServerRefreshResponse:
    server = await _get_owned_server(session, tenant, server_id)
    registry = McpRegistry(session)
    try:
        tool_count = await registry.refresh_tools(server)
    except McpError as exc:
        await session.commit()  # persist the unhealthy status refresh_tools already set
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    await session.commit()
    return McpServerRefreshResponse(tool_count=tool_count)


@router.get("/{server_id}/tools", response_model=list[McpToolResponse])
async def list_server_tools(
    server_id: uuid.UUID,
    tenant: Tenant = Depends(require_tenant),
    session: AsyncSession = Depends(get_session),
) -> list[McpTool]:
    await _get_owned_server(session, tenant, server_id)
    result = await session.execute(
        sa.select(McpTool).where(McpTool.mcp_server_id == server_id).order_by(McpTool.name)
    )
    return list(result.scalars().all())


@router.patch("/{server_id}/tools/{tool_id}", response_model=McpToolResponse)
async def update_server_tool(
    server_id: uuid.UUID,
    tool_id: uuid.UUID,
    body: McpToolUpdateRequest,
    tenant: Tenant = Depends(require_tenant),
    session: AsyncSession = Depends(get_session),
) -> McpTool:
    await _get_owned_server(session, tenant, server_id)
    result = await session.execute(
        sa.select(McpTool).where(McpTool.id == tool_id, McpTool.mcp_server_id == server_id)
    )
    tool = result.scalar_one_or_none()
    if tool is None:
        raise HTTPException(status_code=404, detail="mcp tool not found")

    if body.risk_tier is not None:
        tool.risk_tier = body.risk_tier
    if body.enabled is not None:
        tool.enabled = body.enabled

    await session.commit()
    return tool
