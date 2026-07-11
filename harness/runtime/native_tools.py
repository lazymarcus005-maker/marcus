from typing import Any

from harness.db.enums import RiskTier
from harness.runtime.tools import Tool

FINISH_TOOL_NAME = "finish"
ASK_USER_TOOL_NAME = "ask_user"

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
