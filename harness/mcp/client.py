from contextlib import AsyncExitStack
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import CallToolResult, TextContent
from mcp.types import Tool as McpToolSpec

from harness.db.models import McpServer
from harness.mcp import crypto


class McpError(Exception):
    """Raised on MCP transport/protocol failures, distinct from a normal tool error."""


def _headers(server: McpServer) -> dict[str, str]:
    if server.auth_header_name is None or server.auth_header_value_encrypted is None:
        return {}
    return {server.auth_header_name: crypto.decrypt(server.auth_header_value_encrypted)}


class McpClient:
    """Connect-per-call MCP-over-HTTP client (decisions.md Q10 — simplest first;

    pool later if latency demands it). Every call opens a fresh streamable-HTTP
    session against the server and tears it down when done — there's no
    persistent connection for a stateless worker to lose track of.
    """

    async def list_tools(self, server: McpServer) -> list[McpToolSpec]:
        async with self._session(server) as session:
            try:
                result = await session.list_tools()
            except Exception as exc:  # noqa: BLE001 - normalized into McpError for callers
                raise McpError(f"MCP list_tools failed: {exc}") from exc
            return result.tools

    async def call_tool(self, server: McpServer, name: str, arguments: dict[str, Any]) -> dict:
        async with self._session(server) as session:
            try:
                result = await session.call_tool(name, arguments)
            except Exception as exc:  # noqa: BLE001 - normalized into McpError for callers
                raise McpError(f"MCP call_tool({name!r}) failed: {exc}") from exc
        return _normalize_result(result)

    def _session(self, server: McpServer):
        return _McpSessionContext(server)


class _McpSessionContext:
    """Opens the streamable-HTTP transport, initializes a ClientSession, and

    tears both down on exit. A small helper class rather than a bare
    @asynccontextmanager so it can be reused by both list_tools and
    call_tool without duplicating the nested-context-manager plumbing.
    """

    def __init__(self, server: McpServer) -> None:
        self._server = server
        self._stack = AsyncExitStack()

    async def __aenter__(self) -> ClientSession:
        try:
            read_stream, write_stream, _ = await self._stack.enter_async_context(
                streamablehttp_client(self._server.base_url, headers=_headers(self._server))
            )
            session = await self._stack.enter_async_context(ClientSession(read_stream, write_stream))
            await session.initialize()
            return session
        except Exception as exc:  # noqa: BLE001 - normalized into McpError for callers
            await self._stack.aclose()
            raise McpError(f"failed to connect to MCP server {self._server.name!r}: {exc}") from exc

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self._stack.aclose()


def _normalize_result(result: CallToolResult) -> dict:
    """MCP "Normalize" step (idea.md §5): flatten a CallToolResult's content

    blocks into plain text and surface isError, so downstream (result_pipeline,
    tool_executor) only ever deals with plain dicts regardless of tool source.
    """
    text_parts = [block.text for block in result.content if isinstance(block, TextContent)]
    other_blocks = [block.type for block in result.content if not isinstance(block, TextContent)]
    normalized: dict[str, Any] = {"content": "\n".join(text_parts), "is_error": bool(result.isError)}
    if other_blocks:
        normalized["non_text_content_types"] = other_blocks
    if result.structuredContent is not None:
        normalized["structured_content"] = result.structuredContent
    return normalized
