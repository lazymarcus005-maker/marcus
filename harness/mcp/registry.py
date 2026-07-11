import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from harness.db.enums import McpHealthStatus, RiskTier
from harness.db.models import McpServer, McpTool
from harness.mcp import crypto
from harness.mcp.client import McpClient, McpError


class McpRegistry:
    """DB-backed MCP server/tool registry, used by both the API routes (#14)

    and the engine (#15, to build the tenant's callable tool set).
    """

    def __init__(self, session: AsyncSession, client: McpClient | None = None) -> None:
        self.session = session
        self.client = client or McpClient()

    async def register(
        self,
        *,
        tenant_id: uuid.UUID,
        name: str,
        base_url: str,
        auth_header_name: str | None = None,
        auth_header_value: str | None = None,
        default_risk_tier: RiskTier = RiskTier.read_only,
    ) -> McpServer:
        server = McpServer(
            tenant_id=tenant_id,
            name=name,
            base_url=base_url,
            auth_header_name=auth_header_name,
            auth_header_value_encrypted=(
                crypto.encrypt(auth_header_value) if auth_header_value else None
            ),
            default_risk_tier=default_risk_tier,
        )
        self.session.add(server)
        await self.session.flush()
        return server

    async def list_servers(self, tenant_id: uuid.UUID) -> list[McpServer]:
        result = await self.session.execute(
            sa.select(McpServer).where(McpServer.tenant_id == tenant_id).order_by(McpServer.name)
        )
        return list(result.scalars().all())

    async def get_server(self, tenant_id: uuid.UUID, server_id: uuid.UUID) -> McpServer | None:
        result = await self.session.execute(
            sa.select(McpServer).where(
                McpServer.id == server_id, McpServer.tenant_id == tenant_id
            )
        )
        return result.scalar_one_or_none()

    async def list_tools(self, tenant_id: uuid.UUID) -> list[tuple[McpServer, McpTool]]:
        """All enabled tools across all enabled servers for a tenant."""
        result = await self.session.execute(
            sa.select(McpServer, McpTool)
            .join(McpTool, McpTool.mcp_server_id == McpServer.id)
            .where(
                McpServer.tenant_id == tenant_id,
                McpServer.enabled.is_(True),
                McpTool.enabled.is_(True),
            )
        )
        return [(row.McpServer, row.McpTool) for row in result]

    async def get_tool_by_name(self, tenant_id: uuid.UUID, name: str) -> McpTool | None:
        result = await self.session.execute(
            sa.select(McpTool)
            .join(McpServer, McpTool.mcp_server_id == McpServer.id)
            .where(McpServer.tenant_id == tenant_id, McpTool.name == name)
        )
        return result.scalar_one_or_none()

    async def refresh_tools(self, server: McpServer) -> int:
        """Call tools/list on the server and upsert the results into mcp_tools.

        Returns the number of tools discovered. On transport failure, marks
        the server unhealthy and re-raises so the caller (an API route) can
        surface the error — discovery failures shouldn't be swallowed.
        """
        try:
            discovered = await self.client.list_tools(server)
        except McpError as exc:
            server.health_status = McpHealthStatus.unhealthy
            server.last_error = str(exc)
            server.last_health_checked_at = datetime.now(UTC)
            await self.session.flush()
            raise

        existing_result = await self.session.execute(
            sa.select(McpTool).where(McpTool.mcp_server_id == server.id)
        )
        existing_by_name = {t.name: t for t in existing_result.scalars().all()}
        seen_names: set[str] = set()

        for tool_spec in discovered:
            seen_names.add(tool_spec.name)
            existing = existing_by_name.get(tool_spec.name)
            if existing is not None:
                existing.description = tool_spec.description or ""
                existing.parameters = tool_spec.inputSchema or {}
            else:
                self.session.add(
                    McpTool(
                        mcp_server_id=server.id,
                        name=tool_spec.name,
                        description=tool_spec.description or "",
                        parameters=tool_spec.inputSchema or {},
                        risk_tier=server.default_risk_tier,
                    )
                )

        # Tools no longer reported by the server are disabled, not deleted —
        # preserves tool_executions/mcp_tool_id history and any past risk-tier
        # override if the server re-adds the same tool later.
        for name, tool in existing_by_name.items():
            if name not in seen_names:
                tool.enabled = False

        server.health_status = McpHealthStatus.healthy
        server.last_error = None
        server.last_health_checked_at = datetime.now(UTC)
        await self.session.flush()
        return len(discovered)

    async def check_health(self, server: McpServer) -> McpHealthStatus:
        try:
            await self.client.list_tools(server)
        except McpError as exc:
            server.health_status = McpHealthStatus.unhealthy
            server.last_error = str(exc)
        else:
            server.health_status = McpHealthStatus.healthy
            server.last_error = None
        server.last_health_checked_at = datetime.now(UTC)
        await self.session.flush()
        return server.health_status
