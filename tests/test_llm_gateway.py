import httpx
import orjson
import pytest

from harness.llm.gateway import (
    LLMError,
    LLMGateway,
    LLMToolCallingNotSupportedError,
    LLMTransientError,
)
from harness.llm.types import LLMMessage, ToolSpec


def _openai_response(
    *, content: str | None = "hello", tool_calls: list[dict] | None = None, finish_reason="stop"
) -> dict:
    message = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "id": "chatcmpl-1",
        "model": "gpt-oss:120b",
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


def _client_with_handler(handler) -> httpx.AsyncClient:
    transport = httpx.MockTransport(handler)
    return httpx.AsyncClient(base_url="https://fake-llm.example/v1", transport=transport)


@pytest.mark.asyncio
async def test_complete_parses_basic_response():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_openai_response(content="hi there"))

    gateway = LLMGateway(http_client=_client_with_handler(handler))
    response = await gateway.complete([LLMMessage(role="user", content="hello")])

    assert response.content == "hi there"
    assert response.tool_calls == []
    assert response.usage.total_tokens == 15
    assert response.model == "gpt-oss:120b"


@pytest.mark.asyncio
async def test_complete_stream_emits_text_deltas():
    body = "\n".join(
        [
            'data: {"choices":[{"delta":{"content":"hello "}}]}',
            'data: {"choices":[{"delta":{"content":"world"},"finish_reason":"stop"}]}',
            "data: [DONE]",
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body)

    deltas = []
    gateway = LLMGateway(http_client=_client_with_handler(handler))
    response = await gateway.complete_stream(
        [LLMMessage(role="user", content="hello")], on_delta=deltas.append
    )
    assert response.content == "hello world"
    assert deltas == ["hello ", "world"]


@pytest.mark.asyncio
async def test_complete_stream_assembles_tool_call_fragments():
    body = "\n".join(
        [
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","function":{"name":"search"}}]}}]}',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\\"q\\":\\"x\\"}"}}]},"finish_reason":"tool_calls"}]}',
            "data: [DONE]",
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body)

    gateway = LLMGateway(http_client=_client_with_handler(handler))
    response = await gateway.complete_stream([LLMMessage(role="user", content="search")])
    assert response.tool_calls[0].name == "search"
    assert response.tool_calls[0].arguments == {"q": "x"}


@pytest.mark.asyncio
async def test_complete_stream_retries_on_5xx_then_succeeds(monkeypatch):
    monkeypatch.setattr("harness.llm.gateway.asyncio.sleep", _no_sleep)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(500, text="temporary failure")
        return httpx.Response(
            200,
            text='data: {"choices":[{"delta":{"content":"recovered"}}]}\n\ndata: [DONE]',
        )

    gateway = LLMGateway(http_client=_client_with_handler(handler))
    response = await gateway.complete_stream([LLMMessage(role="user", content="hello")])

    assert calls["n"] == 2
    assert response.content == "recovered"


@pytest.mark.asyncio
async def test_complete_stream_retries_malformed_sse_json(monkeypatch):
    monkeypatch.setattr("harness.llm.gateway.asyncio.sleep", _no_sleep)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, text="data: {not-json}\n\ndata: [DONE]")
        return httpx.Response(
            200,
            text='data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]',
        )

    gateway = LLMGateway(http_client=_client_with_handler(handler))
    response = await gateway.complete_stream([LLMMessage(role="user", content="hello")])

    assert calls["n"] == 2
    assert response.content == "ok"


@pytest.mark.asyncio
async def test_complete_parses_tool_calls():
    tool_calls = [
        {
            "id": "call_1",
            "type": "function",
            "function": {
                "name": "search_logs",
                "arguments": orjson.dumps({"query": "500"}).decode(),
            },
        }
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_openai_response(content=None, tool_calls=tool_calls, finish_reason="tool_calls"),
        )

    gateway = LLMGateway(http_client=_client_with_handler(handler))
    tools = [ToolSpec(name="search_logs", description="search", parameters={"type": "object"})]
    response = await gateway.complete([LLMMessage(role="user", content="find errors")], tools=tools)

    assert response.finish_reason == "tool_calls"
    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].name == "search_logs"
    assert response.tool_calls[0].arguments == {"query": "500"}


@pytest.mark.asyncio
async def test_complete_retries_on_5xx_then_succeeds(monkeypatch):
    monkeypatch.setattr("harness.llm.gateway.asyncio.sleep", _no_sleep)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, text="service unavailable")
        return httpx.Response(200, json=_openai_response())

    gateway = LLMGateway(http_client=_client_with_handler(handler))
    response = await gateway.complete([LLMMessage(role="user", content="hi")], max_retries=5)

    assert calls["n"] == 3
    assert response.content == "hello"


@pytest.mark.asyncio
async def test_complete_retries_on_proxy_error_then_succeeds(monkeypatch):
    monkeypatch.setattr("harness.llm.gateway.asyncio.sleep", _no_sleep)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 2:
            raise httpx.ProxyError("502 Bad Gateway")
        return httpx.Response(200, json=_openai_response())

    gateway = LLMGateway(http_client=_client_with_handler(handler))
    response = await gateway.complete([LLMMessage(role="user", content="hi")], max_retries=3)

    assert calls["n"] == 2
    assert response.content == "hello"


@pytest.mark.asyncio
async def test_complete_raises_transient_error_after_max_retries(monkeypatch):
    monkeypatch.setattr("harness.llm.gateway.asyncio.sleep", _no_sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="service unavailable")

    gateway = LLMGateway(http_client=_client_with_handler(handler))
    with pytest.raises(LLMTransientError):
        await gateway.complete([LLMMessage(role="user", content="hi")], max_retries=2)


@pytest.mark.asyncio
async def test_complete_raises_and_caches_tool_calling_not_supported():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, text="this model does not support tools")

    gateway = LLMGateway(http_client=_client_with_handler(handler))
    tools = [ToolSpec(name="x", description="x", parameters={"type": "object"})]

    with pytest.raises(LLMToolCallingNotSupportedError):
        await gateway.complete([LLMMessage(role="user", content="hi")], tools=tools)

    # Second call should fail fast without another HTTP request.
    with pytest.raises(LLMToolCallingNotSupportedError):
        await gateway.complete([LLMMessage(role="user", content="hi")], tools=tools)

    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_complete_raises_llm_error_on_non_retryable_status():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="invalid api key")

    gateway = LLMGateway(http_client=_client_with_handler(handler))
    with pytest.raises(LLMError):
        await gateway.complete([LLMMessage(role="user", content="hi")])


async def _no_sleep(_seconds: float) -> None:
    return None
