import asyncio
import random
from collections.abc import Callable
from typing import Any

import httpx
import orjson

from harness.config import Settings, get_settings
from harness.llm.providers import AdaptedRequest, adapt_request, provider_message_fields
from harness.llm.types import LLMMessage, LLMOptions, LLMResponse, ToolCall, ToolSpec, Usage

RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})


class LLMError(Exception):
    """Base class for LLM Gateway errors."""


class LLMToolCallingNotSupportedError(LLMError):
    """Raised when the configured model does not support tool-calling."""


class LLMTransientError(LLMError):
    """Raised when the gateway exhausts retries on a transient failure."""


class _LLMReasoningNotSupported(LLMError):
    """Internal signal used to retry once without provider reasoning controls."""


def _looks_like_tool_unsupported(status_code: int, body: str) -> bool:
    if status_code != 400:
        return False
    lowered = body.lower()
    return any(
        phrase in lowered
        for phrase in (
            "does not support tools",
            "tools are not supported",
            "tool calling is not supported",
            "function calling",
            "function_call",
        )
    )


def _looks_like_reasoning_unsupported(
    status_code: int, body: str, reasoning_fields: frozenset[str]
) -> bool:
    if status_code not in {400, 422} or not reasoning_fields:
        return False
    lowered = body.lower()
    field_mentioned = any(field.lower() in lowered for field in reasoning_fields)
    reasoning_mentioned = any(
        phrase in lowered
        for phrase in (
            "reasoning",
            "thinking",
            "chat_template_kwargs",
            "nvext",
        )
    )
    rejection_mentioned = any(
        phrase in lowered
        for phrase in (
            "unknown",
            "unsupported",
            "unrecognized",
            "not permitted",
            "extra inputs",
            "invalid parameter",
            "unexpected keyword",
        )
    )
    return rejection_mentioned and (field_mentioned or reasoning_mentioned)


class LLMGateway:
    """OpenAI-compatible chat completions client (decisions.md D2).

    Supports both complete responses and OpenAI-compatible SSE streaming.
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
        self._reasoning_capable: dict[
            tuple[str, str, frozenset[str], str | None], bool
        ] = {}

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def complete(
        self,
        messages: list[LLMMessage],
        *,
        tools: list[ToolSpec] | None = None,
        model: str | None = None,
        options: LLMOptions | None = None,
        temperature: float = 0.0,
        max_retries: int = 3,
    ) -> LLMResponse:
        if tools and self._tool_capable is False:
            raise LLMToolCallingNotSupportedError(
                f"model {model or self._settings.llm_model!r} was already found to not "
                "support tool-calling in a prior call"
            )

        model_name = model or self._settings.llm_model
        message_fields = provider_message_fields(
            self._settings.llm_base_url, model_name, self._settings.llm_provider
        )
        base_payload: dict[str, Any] = {
            "model": model_name,
            "messages": [m.to_openai(provider_fields=message_fields) for m in messages],
            "temperature": temperature,
        }
        if tools:
            base_payload["tools"] = [t.to_openai() for t in tools]
        adapted = adapt_request(
            base_payload,
            base_url=self._settings.llm_base_url,
            configured_provider=self._settings.llm_provider,
            options=options,
        )
        capability_key = self._reasoning_cache_key(adapted, model_name)
        payload, reasoning_fields = self._select_adapted_payload(adapted, model_name)

        try:
            response = await self._post_with_retry(
                payload, max_retries=max_retries, reasoning_fields=reasoning_fields
            )
        except _LLMReasoningNotSupported:
            self._reasoning_capable[capability_key] = False
            payload = adapted.fallback_payload
            response = await self._post_with_retry(payload, max_retries=max_retries)
        else:
            if reasoning_fields:
                self._reasoning_capable[capability_key] = True
        body = response.json()

        if tools:
            self._tool_capable = True

        return _ensure_usage(_parse_response(body), payload)

    async def complete_stream(
        self,
        messages: list[LLMMessage],
        *,
        tools: list[ToolSpec] | None = None,
        model: str | None = None,
        options: LLMOptions | None = None,
        on_delta: Callable[[str], None] | None = None,
        max_retries: int = 3,
    ) -> LLMResponse:
        """Stream text deltas while assembling a normal LLMResponse.

        Tool-call fragments are accumulated and parsed at the end, so callers
        can use the same ReAct path as ``complete``.
        """
        model_name = model or self._settings.llm_model
        message_fields = provider_message_fields(
            self._settings.llm_base_url, model_name, self._settings.llm_provider
        )
        base_payload: dict[str, Any] = {
            "model": model_name,
            "messages": [m.to_openai(provider_fields=message_fields) for m in messages],
            "temperature": 0.0,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            base_payload["tools"] = [t.to_openai() for t in tools]
        adapted = adapt_request(
            base_payload,
            base_url=self._settings.llm_base_url,
            configured_provider=self._settings.llm_provider,
            options=options,
        )
        capability_key = self._reasoning_cache_key(adapted, model_name)
        payload, reasoning_fields = self._select_adapted_payload(adapted, model_name)
        attempt = 0
        fell_back = not reasoning_fields and bool(adapted.reasoning_fields)
        while True:
            try:
                response = await self._complete_stream_once(
                    payload,
                    model=model,
                    on_delta=on_delta,
                    reasoning_fields=reasoning_fields,
                )
                if reasoning_fields:
                    self._reasoning_capable[capability_key] = True
                return response
            except _LLMReasoningNotSupported:
                if fell_back:
                    raise
                self._reasoning_capable[capability_key] = False
                payload = adapted.fallback_payload
                reasoning_fields = frozenset()
                fell_back = True
            except (httpx.TransportError, LLMTransientError) as exc:
                if attempt >= max_retries:
                    raise LLMTransientError(
                        f"LLM streaming request failed after {attempt + 1} attempts: {exc}"
                    ) from exc
                await asyncio.sleep(_backoff_delay(attempt))
                attempt += 1

    async def _complete_stream_once(
        self,
        payload: dict[str, Any],
        *,
        model: str | None,
        on_delta: Callable[[str], None] | None,
        reasoning_fields: frozenset[str] = frozenset(),
    ) -> LLMResponse:
        content_parts: list[str] = []
        reasoning_parts: dict[str, list[str]] = {"reasoning": [], "reasoning_content": []}
        reasoning_details: list[Any] = []
        calls: dict[int, dict[str, str]] = {}
        usage = Usage(0, 0, 0)
        finish_reason = "stop"
        async with self._client.stream("POST", "/chat/completions", json=payload) as response:
            if response.status_code != 200:
                await response.aread()
                if response.status_code in RETRYABLE_STATUS_CODES:
                    raise LLMTransientError(f"status {response.status_code}: {response.text[:500]}")
                if _looks_like_reasoning_unsupported(
                    response.status_code, response.text, reasoning_fields
                ):
                    raise _LLMReasoningNotSupported(response.text[:500])
                raise LLMError(
                    f"LLM streaming request failed with status {response.status_code}: "
                    f"{response.text[:500]}"
                )
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                if raw == "[DONE]":
                    break
                try:
                    chunk = orjson.loads(raw)
                except orjson.JSONDecodeError as exc:
                    raise LLMTransientError("LLM stream returned malformed JSON") from exc
                choice = (chunk.get("choices") or [{}])[0]
                finish_reason = choice.get("finish_reason") or finish_reason
                delta = choice.get("delta") or {}
                text = delta.get("content") or ""
                if text:
                    content_parts.append(text)
                    if on_delta:
                        on_delta(text)
                for field in reasoning_parts:
                    reasoning_text = delta.get(field)
                    if isinstance(reasoning_text, str) and reasoning_text:
                        reasoning_parts[field].append(reasoning_text)
                details = delta.get("reasoning_details")
                if isinstance(details, list):
                    reasoning_details.extend(details)
                for fragment in delta.get("tool_calls") or []:
                    index = fragment.get("index", 0)
                    entry = calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
                    entry["id"] += fragment.get("id") or ""
                    function = fragment.get("function") or {}
                    entry["name"] += function.get("name") or ""
                    entry["arguments"] += function.get("arguments") or ""
                usage_raw = chunk.get("usage") or {}
                if usage_raw:
                    usage = Usage(
                        usage_raw.get("prompt_tokens", 0),
                        usage_raw.get("completion_tokens", 0),
                        usage_raw.get("total_tokens", 0),
                        reasoning_tokens=_reasoning_tokens(usage_raw),
                    )
        tool_calls = [
            ToolCall(id=v["id"], name=v["name"], arguments=orjson.loads(v["arguments"] or "{}"))
            for v in calls.values()
        ]
        provider_fields: dict[str, Any] = {
            field: "".join(parts) for field, parts in reasoning_parts.items() if parts
        }
        if reasoning_details:
            provider_fields["reasoning_details"] = reasoning_details
        llm_response = LLMResponse(
            "".join(content_parts) or None,
            tool_calls,
            finish_reason,
            usage,
            model or self._settings.llm_model,
            {},
            provider_fields,
        )
        return _ensure_usage(llm_response, payload)

    def _select_adapted_payload(
        self, adapted: AdaptedRequest, model: str
    ) -> tuple[dict[str, Any], frozenset[str]]:
        if self._reasoning_capable.get(self._reasoning_cache_key(adapted, model)) is False:
            return adapted.fallback_payload, frozenset()
        return adapted.payload, adapted.reasoning_fields

    @staticmethod
    def _reasoning_cache_key(
        adapted: AdaptedRequest, model: str
    ) -> tuple[str, str, frozenset[str], str | None]:
        return adapted.provider, model, adapted.reasoning_fields, adapted.effective_effort

    async def _post_with_retry(
        self,
        payload: dict[str, Any],
        *,
        max_retries: int,
        reasoning_fields: frozenset[str] = frozenset(),
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

            if _looks_like_reasoning_unsupported(
                response.status_code, response.text, reasoning_fields
            ):
                raise _LLMReasoningNotSupported(response.text[:500])

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
        reasoning_tokens=_reasoning_tokens(usage_raw),
    )

    provider_fields = {
        key: message[key]
        for key in ("reasoning", "reasoning_content", "reasoning_details")
        if message.get(key) is not None
    }

    return LLMResponse(
        content=message.get("content"),
        tool_calls=tool_calls,
        finish_reason=choice.get("finish_reason", "stop"),
        usage=usage,
        model=body.get("model", ""),
        raw=body,
        provider_fields=provider_fields,
    )


def _reasoning_tokens(usage: dict[str, Any]) -> int:
    details = usage.get("completion_tokens_details") or {}
    if not isinstance(details, dict):
        return 0
    value = details.get("reasoning_tokens", 0)
    return value if isinstance(value, int) and value >= 0 else 0


def _ensure_usage(response: LLMResponse, payload: dict[str, Any]) -> LLMResponse:
    """Estimate non-zero usage when an OpenAI-compatible provider omits it."""
    if response.usage.total_tokens > 0:
        return response

    prompt_bytes = len(orjson.dumps(payload.get("messages", [])))
    if payload.get("tools"):
        prompt_bytes += len(orjson.dumps(payload["tools"]))
    completion_bytes = len((response.content or "").encode())
    completion_bytes += sum(
        len(call.name.encode()) + len(orjson.dumps(call.arguments)) for call in response.tool_calls
    )
    prompt_tokens = max(1, (prompt_bytes + 3) // 4)
    completion_tokens = max(1, (completion_bytes + 3) // 4)
    response.usage = Usage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
        source="estimated",
    )
    return response
