import asyncio
import itertools
import uuid
from typing import TYPE_CHECKING

from harness.llm.types import LLMResponse, ToolCall, Usage

if TYPE_CHECKING:
    import aio_pika

_call_id_counter = itertools.count(1)


class ScriptedLLMGateway:
    """A fake LLMGateway that plays back a fixed sequence of responses.

    Each call to complete() returns the next response in the script,
    regardless of the messages/tools passed in. Records every call's
    messages for assertions.
    """

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = iter(responses)
        self.calls: list[dict] = []

    async def complete(self, messages, *, tools=None, model=None, temperature=0.0, max_retries=3):
        self.calls.append({"messages": messages, "tools": tools})
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
