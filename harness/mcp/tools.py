from typing import Any

from harness.db.models import McpServer, McpTool
from harness.mcp.client import McpClient, McpError
from harness.runtime.tools import Tool, ToolErrorCode, ToolRuntimeError


def canonical_tool_name(server: McpServer, tool_name: str) -> str:
    """Stable collision-safe identifier used when two MCP domains reuse a name."""
    safe_server = "".join(char if char.isalnum() or char in "_-" else "_" for char in server.name)
    return f"{safe_server}__{tool_name}"


def build_tool(
    server: McpServer,
    mcp_tool: McpTool,
    client: McpClient,
    *,
    exposed_name: str | None = None,
) -> Tool:
    """Wrap a discovered McpTool DB row into the engine's Tool abstraction.

    The handler calls the MCP server and normalizes any transport-level
    failure (McpError) into an observation dict rather than letting it
    propagate — a downed MCP server is a tool failure the LLM should see and
    react to, not a crash of the run (consistent with how ToolExecutor treats
    every other handler exception).
    """

    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            result = await client.call_tool(server, mcp_tool.name, arguments)
        except McpError as exc:
            raise ToolRuntimeError(
                str(exc), code=ToolErrorCode.execution_failed, retryable=True
            ) from exc
        if result.get("is_error") or "error" in result:
            raise ToolRuntimeError(
                str(result.get("error") or result.get("content") or "MCP tool failed"),
                code=ToolErrorCode.execution_failed,
                retryable=True,
            )
        return result

    return Tool(
        name=exposed_name or mcp_tool.name,
        description=mcp_tool.description,
        parameters=mcp_tool.parameters,
        handler=handler,
        risk_tier=mcp_tool.risk_tier,
        idempotent=False,
        mcp_server_id=server.id,
    )
