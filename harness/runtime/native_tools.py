from typing import Any

from harness.db.enums import RiskTier
from harness.runtime.tools import Tool, ToolHandler

FINISH_TOOL_NAME = "finish"
ASK_USER_TOOL_NAME = "ask_user"
LIST_TOOL_DOMAINS_NAME = "list_tool_domains"
LIST_DOMAIN_TOOLS_NAME = "list_domain_tools"
LOAD_TOOL_NAME = "load_tool"

FINISH_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "result": {"description": "The final result or answer for the goal."},
        "summary": {"type": "string", "description": "Brief summary of what was done."},
    },
    "required": ["result"],
}

ASK_USER_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "question": {"type": "string", "description": "The question to ask the user."},
    },
    "required": ["question"],
}

LIST_TOOL_DOMAINS_SCHEMA = {"type": "object", "properties": {}, "required": []}

LIST_DOMAIN_TOOLS_SCHEMA = {
    "type": "object",
    "properties": {
        "domain": {
            "type": "string",
            "description": "A domain name returned by list_tool_domains.",
        },
    },
    "required": ["domain"],
}

LOAD_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": "A tool name returned by list_domain_tools.",
        },
    },
    "required": ["name"],
}


async def _finish_handler(arguments: dict[str, Any]) -> dict[str, Any]:
    if "result" not in arguments:
        raise ValueError("finish requires a 'result' field")
    return arguments


async def _ask_user_handler(arguments: dict[str, Any]) -> dict[str, Any]:
    if "question" not in arguments:
        raise ValueError("ask_user requires a 'question' field")
    return arguments


def build_finish_tool() -> Tool:
    return Tool(
        name=FINISH_TOOL_NAME,
        description=(
            "Call this when the goal has been achieved. Pass the final result and a "
            "brief summary of what was done."
        ),
        parameters=FINISH_TOOL_SCHEMA,
        handler=_finish_handler,
        risk_tier=RiskTier.read_only,
        idempotent=True,
    )


def build_ask_user_tool() -> Tool:
    return Tool(
        name=ASK_USER_TOOL_NAME,
        description="Call this to ask the user a clarifying question before continuing.",
        parameters=ASK_USER_TOOL_SCHEMA,
        handler=_ask_user_handler,
        risk_tier=RiskTier.read_only,
        idempotent=True,
    )


# Progressive tool disclosure (issue #15, idea.md §4): these three meta-tools
# expose the tool catalog in three levels (domain -> tool summary -> full
# schema) instead of sending every MCP tool's full schema to the LLM up
# front. Their real logic needs DB access the tenant's MCP registry, which a
# plain ToolHandler (arguments-only) doesn't have — RunEngine builds the
# actual handler per call (closed over the run's tenant) and passes it in
# here, mirroring the shape of build_finish_tool/build_ask_user_tool so the
# resulting Tool still flows through the normal write-ahead/idempotency path.


def build_list_tool_domains_tool(handler: ToolHandler) -> Tool:
    return Tool(
        name=LIST_TOOL_DOMAINS_NAME,
        description=(
            "List the available tool domains (one per registered external system, "
            "e.g. an MCP server). Call this first to discover what capabilities "
            "exist before you know which tool you need."
        ),
        parameters=LIST_TOOL_DOMAINS_SCHEMA,
        handler=handler,
        risk_tier=RiskTier.read_only,
        idempotent=True,
    )


def build_list_domain_tools_tool(handler: ToolHandler) -> Tool:
    return Tool(
        name=LIST_DOMAIN_TOOLS_NAME,
        description=(
            "List the tools available within one domain: name and a short summary "
            "for each, without full parameter schemas. Call load_tool on one of "
            "them to get its schema before calling it."
        ),
        parameters=LIST_DOMAIN_TOOLS_SCHEMA,
        handler=handler,
        risk_tier=RiskTier.read_only,
        idempotent=True,
    )


def build_load_tool_tool(handler: ToolHandler) -> Tool:
    return Tool(
        name=LOAD_TOOL_NAME,
        description=(
            "Unlock a specific tool by name: returns its full parameter schema and "
            "makes it callable starting next turn. You must call this before "
            "calling any tool other than finish, ask_user, or these meta-tools."
        ),
        parameters=LOAD_TOOL_SCHEMA,
        handler=handler,
        risk_tier=RiskTier.read_only,
        idempotent=True,
    )
