import asyncio
import random
from typing import Any

import httpx
import orjson

from harness.config import Settings, get_settings
from harness.llm.types import LLMMessage, LLMResponse, ToolCall, ToolSpec, Usage

RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})


class LLMError(Exception):
    """Base class for LLM Gateway errors."""


class LLMToolCallingNotSupportedError(LLMError):
    """Raised when the configured model does not support tool-calling."""


class LLMTransientError(LLMError):
    """Raised when the gateway exhausts retries on a transient failure."""


def _looks_like_tool_unsupported(status_code: int, body: str) -> bool:
    if status_code != 400:
        return False
    lowered = body.lower()
    return any(
        phrase in lowered
        for phrase in ("does not support tools", "tool", "function calling", "function_call")
    )


class LLMGateway:
    """OpenAI-compatible chat completions client (decisions.md D2).

    No streaming (decisions.md D7) — every call blocks for the full response.
    """

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._client = http_client or httpx.AsyncClient(
            base_url=self._settings.llm_base_url,
            headers={"Authorization": f"Bearer {self._settings.llm_api_key}"},
            timeout=self._settings.llm_timeout_seconds,
        )
        self._owns_client = http_client is None
        self._tool_capable: bool | None = None  # None = unknown, cached after first signal

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def complete(
        self,
        messages: list[LLMMessage],
        *,
        tools: list[ToolSpec] | None = None,
        model: str | None = None,
        temperature: float = 0.0,
        max_retries: int = 3,
    ) -> LLMResponse:
        if tools and self._tool_capable is False:
            raise LLMToolCallingNotSupportedError(
                f"model {model or self._settings.llm_model!r} was already found to not "
                "support tool-calling in a prior call"
            )

        payload: dict[str, Any] = {
            "model": model or self._settings.llm_model,
            "messages": [m.to_openai() for m in messages],
            "temperature": temperature,
        }
        if tools:
            payload["tools"] = [t.to_openai() for t in tools]

        response = await self._post_with_retry(payload, max_retries=max_retries)
        body = response.json()

        if tools:
            self._tool_capable = True

        return _parse_response(body)

    async def _post_with_retry(
        self, payload: dict[str, Any], *, max_retries: int
    ) -> httpx.Response:
        attempt = 0
        while True:
            try:
                response = await self._client.post("/chat/completions", json=payload)
            except httpx.TransportError as exc:
                # Base class for every network-layer failure (connect/read/write
                # timeouts, connection resets, proxy errors, ...) — narrower
                # tuples here miss real failure modes (e.g. httpx.ProxyError)
                # and let them escape complete() unwrapped, which breaks the
                # engine's contract that only LLMError subclasses cross this
                # boundary.
                if attempt >= max_retries:
                    raise LLMTransientError(
                        f"LLM request failed after {attempt + 1} attempts: {exc}"
                    ) from exc
                await asyncio.sleep(_backoff_delay(attempt))
                attempt += 1
                continue

            if response.status_code == 200:
                return response

            if "tools" in payload and _looks_like_tool_unsupported(
                response.status_code, response.text
            ):
                self._tool_capable = False
                raise LLMToolCallingNotSupportedError(
                    f"model {payload['model']!r} does not support tool-calling: "
                    f"{response.text[:500]}"
                )

            if response.status_code in RETRYABLE_STATUS_CODES:
                if attempt < max_retries:
                    await asyncio.sleep(_backoff_delay(attempt))
                    attempt += 1
                    continue
                raise LLMTransientError(
                    f"LLM request failed after {attempt + 1} attempts with status "
                    f"{response.status_code}: {response.text[:500]}"
                )

            raise LLMError(
                f"LLM request failed with status {response.status_code}: {response.text[:500]}"
            )


def _backoff_delay(attempt: int) -> float:
    base = min(2**attempt, 30)
    return base + random.uniform(0, base * 0.1)


def _parse_response(body: dict[str, Any]) -> LLMResponse:
    choice = body["choices"][0]
    message = choice["message"]

    tool_calls = [
        ToolCall(
            id=call["id"],
            name=call["function"]["name"],
            arguments=orjson.loads(call["function"]["arguments"] or "{}"),
        )
        for call in message.get("tool_calls") or []
    ]

    usage_raw = body.get("usage") or {}
    usage = Usage(
        prompt_tokens=usage_raw.get("prompt_tokens", 0),
        completion_tokens=usage_raw.get("completion_tokens", 0),
        total_tokens=usage_raw.get("total_tokens", 0),
    )

    return LLMResponse(
        content=message.get("content"),
        tool_calls=tool_calls,
        finish_reason=choice.get("finish_reason", "stop"),
        usage=usage,
        model=body.get("model", ""),
        raw=body,
    )
