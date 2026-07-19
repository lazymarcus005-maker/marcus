import asyncio
import itertools
import uuid
from typing import TYPE_CHECKING, Any

from harness.llm.types import LLMResponse, ToolCall, Usage

if TYPE_CHECKING:
    import aio_pika

_call_id_counter = itertools.count(1)


class FakeMcpClient:
    """Fake harness.mcp.client.McpClient — scripted tools/results, no network I/O.

    tools_by_server maps server.name -> list[mcp.types.Tool]; call_results
    maps tool name -> either a result dict or an Exception instance to raise
    (call_tool normalizes real McpErrors into {"error": ...} dicts, same as
    the real client's callers do — see harness/mcp/tools.py).
    """

    def __init__(
        self,
        tools_by_server: dict[str, list] | None = None,
        call_results: dict[str, Any] | None = None,
    ) -> None:
        self.tools_by_server = tools_by_server or {}
        self.call_results = call_results or {}
        self.calls: list[tuple[str, str, dict]] = []

    async def list_tools(self, server) -> list:
        return self.tools_by_server.get(server.name, [])

    async def call_tool(self, server, name: str, arguments: dict) -> dict:
        self.calls.append((server.name, name, arguments))
        result = self.call_results.get(name, {"content": "ok", "is_error": False})
        if isinstance(result, Exception):
            raise result
        return result


class ScriptedLLMGateway:
    """A fake LLMGateway that plays back a fixed sequence of responses.

    Each call to complete() returns the next response in the script,
    regardless of the messages/tools passed in. Records every call's
    messages for assertions.
    """

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = iter(responses)
        self.calls: list[dict] = []

    async def complete(
        self,
        messages,
        *,
        tools=None,
        model=None,
        options=None,
        temperature=0.0,
        max_retries=3,
    ):
        self.calls.append({"messages": messages, "tools": tools, "options": options})
        try:
            return next(self._responses)
        except StopIteration as exc:
            raise AssertionError("ScriptedLLMGateway ran out of scripted responses") from exc


def tool_call_response(
    name: str, arguments: dict, *, content: str | None = None, model: str = "test-model"
) -> LLMResponse:
    call_id = f"call_{next(_call_id_counter)}"
    return LLMResponse(
        content=content,
        tool_calls=[ToolCall(id=call_id, name=name, arguments=arguments)],
        finish_reason="tool_calls",
        usage=Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        model=model,
        raw={},
    )


def text_response(content: str, *, model: str = "test-model") -> LLMResponse:
    return LLMResponse(
        content=content,
        tool_calls=[],
        finish_reason="stop",
        usage=Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        model=model,
        raw={},
    )


def new_uuid() -> uuid.UUID:
    return uuid.uuid4()


async def get_message_with_wait(
    queue: "aio_pika.abc.AbstractQueue", *, timeout: float = 5, poll_interval: float = 0.05
) -> "aio_pika.abc.AbstractIncomingMessage":
    """queue.get(fail=True) is a single non-blocking basic.get RPC, not a wait —

    routing (including dead-lettering after a reject) isn't necessarily
    synchronous with the call that triggered it, so poll instead of relying
    on one immediate attempt.
    """

    async def _poll():
        while True:
            message = await queue.get(fail=False)
            if message is not None:
                return message
            await asyncio.sleep(poll_interval)

    return await asyncio.wait_for(_poll(), timeout=timeout)
