from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any, Protocol, cast
from urllib.parse import urlparse

from harness.llm.types import LLMOptions, LLMProvider, ReasoningEffort

_CORE_REQUEST_FIELDS = frozenset(
    {"model", "messages", "tools", "stream", "stream_options", "temperature"}
)
_OPENROUTER_MESSAGE_FIELDS = frozenset({"reasoning", "reasoning_content", "reasoning_details"})
_NVIDIA_MESSAGE_FIELDS = frozenset({"reasoning", "reasoning_content"})


@dataclass(frozen=True)
class AdaptedRequest:
    payload: dict[str, Any]
    fallback_payload: dict[str, Any]
    provider: LLMProvider
    reasoning_fields: frozenset[str]
    effective_effort: str | None


class ProviderAdapter(Protocol):
    name: LLMProvider
    message_fields: frozenset[str]

    def apply_output_limit(self, payload: dict[str, Any], value: int) -> None: ...

    def apply_reasoning(
        self, payload: dict[str, Any], model: str, options: LLMOptions
    ) -> tuple[set[str], str | None]: ...


class CompatibleAdapter:
    name: LLMProvider = "compatible"
    message_fields = frozenset()

    def apply_output_limit(self, payload: dict[str, Any], value: int) -> None:
        payload["max_tokens"] = value

    def apply_reasoning(
        self, payload: dict[str, Any], model: str, options: LLMOptions
    ) -> tuple[set[str], str | None]:
        return set(), None


class OpenAIAdapter(CompatibleAdapter):
    name: LLMProvider = "openai"

    def apply_output_limit(self, payload: dict[str, Any], value: int) -> None:
        payload["max_completion_tokens"] = value

    def apply_reasoning(
        self, payload: dict[str, Any], model: str, options: LLMOptions
    ) -> tuple[set[str], str | None]:
        effort = _requested_effort(options)
        if effort is None or not _is_openai_reasoning_model(model):
            return set(), None

        effective = _openai_effort(model, effort)
        if effective is None:
            return set(), None
        payload["reasoning_effort"] = effective
        return {"reasoning_effort"}, effective


class OpenRouterAdapter(CompatibleAdapter):
    name: LLMProvider = "openrouter"
    message_fields = _OPENROUTER_MESSAGE_FIELDS

    def apply_reasoning(
        self, payload: dict[str, Any], model: str, options: LLMOptions
    ) -> tuple[set[str], str | None]:
        effort = _requested_effort(options)
        reasoning = _mapping(payload.get("reasoning"))
        if options.reasoning_budget_tokens is not None and effort != "off":
            reasoning.pop("effort", None)
            reasoning["max_tokens"] = options.reasoning_budget_tokens
            effective = None
        elif effort is not None:
            effective = "none" if effort == "off" else effort
            reasoning["effort"] = effective
        else:
            return set(), None
        payload["reasoning"] = reasoning
        return {"reasoning"}, effective


class OllamaAdapter(CompatibleAdapter):
    name: LLMProvider = "ollama"

    def apply_output_limit(self, payload: dict[str, Any], value: int) -> None:
        payload["max_tokens"] = value

    def apply_reasoning(
        self, payload: dict[str, Any], model: str, options: LLMOptions
    ) -> tuple[set[str], str | None]:
        effort = _requested_effort(options)
        if effort is None:
            return set(), None
        # Ollama documents that GPT-OSS only accepts low/medium/high. Its
        # reasoning trace cannot be disabled, so "off" degrades to low.
        effective = "low" if effort == "off" and "gpt-oss" in model.lower() else effort
        if effective == "off":
            effective = "none"
        payload["reasoning_effort"] = effective
        return {"reasoning_effort"}, effective


class NvidiaAdapter(CompatibleAdapter):
    name: LLMProvider = "nvidia"
    message_fields = _NVIDIA_MESSAGE_FIELDS

    def apply_output_limit(self, payload: dict[str, Any], value: int) -> None:
        payload["max_tokens"] = value

    def apply_reasoning(
        self, payload: dict[str, Any], model: str, options: LLMOptions
    ) -> tuple[set[str], str | None]:
        if "nemotron" not in model.lower():
            return set(), None
        effort = _requested_effort(options)
        if effort is None and options.reasoning_budget_tokens is None:
            return set(), None

        effective = effort
        template = _mapping(payload.get("chat_template_kwargs"))
        if effort == "off":
            template["enable_thinking"] = False
            template.pop("low_effort", None)
        else:
            template["enable_thinking"] = True
            template["low_effort"] = effort == "low"
            if options.reasoning_budget_tokens is not None:
                _apply_nemotron_budget(payload, template, model, options.reasoning_budget_tokens)
        payload["chat_template_kwargs"] = template

        fields = {"chat_template_kwargs"}
        fields.update(
            key
            for key in ("thinking_token_budget", "max_thinking_tokens", "nvext")
            if key in payload
        )
        return fields, effective


_ADAPTERS: dict[LLMProvider, ProviderAdapter] = {
    "compatible": CompatibleAdapter(),
    "openai": OpenAIAdapter(),
    "openrouter": OpenRouterAdapter(),
    "ollama": OllamaAdapter(),
    "nvidia": NvidiaAdapter(),
}


def resolve_provider(
    base_url: str, model: str, configured: LLMProvider = "auto"
) -> ProviderAdapter:
    if configured != "auto":
        return _ADAPTERS[configured]

    parsed = urlparse(base_url)
    host = (parsed.hostname or "").lower()
    if host == "openrouter.ai" or host.endswith(".openrouter.ai"):
        return _ADAPTERS["openrouter"]
    if host == "ollama.com" or host.endswith(".ollama.com"):
        return _ADAPTERS["ollama"]
    if host in {"localhost", "127.0.0.1", "::1"} and parsed.port == 11434:
        return _ADAPTERS["ollama"]
    if host == "api.openai.com" or host.endswith(".openai.com"):
        return _ADAPTERS["openai"]
    if "nvidia" in host or host.endswith(".nvcf.nvidia.com") or "nemotron" in model.lower():
        return _ADAPTERS["nvidia"]
    return _ADAPTERS["compatible"]


def adapt_request(
    payload: dict[str, Any],
    *,
    base_url: str,
    configured_provider: LLMProvider,
    options: LLMOptions | None,
) -> AdaptedRequest:
    model = str(payload["model"])
    adapter = resolve_provider(base_url, model, configured_provider)
    adapted = copy.deepcopy(payload)
    if options is None:
        return AdaptedRequest(adapted, copy.deepcopy(adapted), adapter.name, frozenset(), None)

    for key, value in options.extra_body.items():
        if key not in _CORE_REQUEST_FIELDS:
            adapted[key] = copy.deepcopy(value)
    if options.max_completion_tokens is not None:
        adapter.apply_output_limit(adapted, options.max_completion_tokens)

    fallback = copy.deepcopy(adapted)
    reasoning_fields, effective_effort = adapter.apply_reasoning(adapted, model, options)
    return AdaptedRequest(
        adapted,
        fallback,
        adapter.name,
        frozenset(reasoning_fields),
        effective_effort,
    )


def provider_message_fields(
    base_url: str, model: str, configured_provider: LLMProvider
) -> frozenset[str]:
    return resolve_provider(base_url, model, configured_provider).message_fields


def _requested_effort(options: LLMOptions) -> ReasoningEffort | None:
    if options.thinking_enabled is False:
        return "off"
    if options.reasoning_effort != "auto":
        return options.reasoning_effort
    if options.thinking_enabled is True:
        return "medium"
    return None


def _is_openai_reasoning_model(model: str) -> bool:
    name = model.lower().rsplit("/", 1)[-1]
    return bool(re.match(r"^(?:o[134](?:-|$)|gpt-[5-9](?:[.-]|$))", name))


def _openai_effort(model: str, effort: ReasoningEffort) -> str | None:
    name = model.lower().rsplit("/", 1)[-1]
    if "-pro" in name:
        return "high" if effort == "high" else None
    if effort != "off":
        return effort
    match = re.match(r"^gpt-(\d+)(?:\.(\d+))?", name)
    if match:
        major = int(match.group(1))
        minor = int(match.group(2) or 0)
        if major > 5 or (major == 5 and minor >= 1):
            return "none"
        return "minimal"
    # o-series models before GPT-5.1 cannot disable reasoning. Low is the
    # closest supported value and the system hint still requests brevity.
    return "low"


def _apply_nemotron_budget(
    payload: dict[str, Any], template: dict[str, Any], model: str, budget: int
) -> None:
    name = model.lower()
    if "9b-v2" in name:
        nvext = _mapping(payload.get("nvext"))
        nvext["max_thinking_tokens"] = budget
        payload["nvext"] = nvext
    elif "ultra" in name or "omni" in name:
        payload["thinking_token_budget"] = budget
    elif "nano-30b" in name:
        payload["max_thinking_tokens"] = budget
    else:
        template["reasoning_budget"] = budget


def _mapping(value: Any) -> dict[str, Any]:
    return copy.deepcopy(cast(dict[str, Any], value)) if isinstance(value, dict) else {}
