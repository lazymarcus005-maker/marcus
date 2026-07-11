from typing import Any

from harness.db.models import McpServer, McpTool
from harness.mcp.client import McpClient, McpError
from harness.runtime.tools import Tool


def build_tool(server: McpServer, mcp_tool: McpTool, client: McpClient) -> Tool:
    """Wrap a discovered McpTool DB row into the engine's Tool abstraction.

    The handler calls the MCP server and normalizes any transport-level
    failure (McpError) into an observation dict rather than letting it
    propagate — a downed MCP server is a tool failure the LLM should see and
    react to, not a crash of the run (consistent with how ToolExecutor treats
    every other handler exception).
    """

    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            return await client.call_tool(server, mcp_tool.name, arguments)
        except McpError as exc:
            return {"error": str(exc)}

    return Tool(
        name=mcp_tool.name,
        description=mcp_tool.description,
        parameters=mcp_tool.parameters,
        handler=handler,
        risk_tier=mcp_tool.risk_tier,
        idempotent=False,
        mcp_server_id=server.id,
    )
